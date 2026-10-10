"""Provider batches: submitting one, parking the task on it, collecting it,
and recording what it cost.

A batch lives provider-side while it queues, so a worker polling one does no
work. The park in here is what frees the slot, and the batch ids in the task
payload are what lets a resumed run reattach to work already paid for instead
of buying it twice.
"""

from __future__ import annotations

import dataclasses
import logging
from decimal import Decimal
from typing import Any

from api import budget, db, events
from api.ai import batch_results
from api.ai.batch_results import consume_result as consume_result
from api.ai.batch_results import snapshot_specs as snapshot_specs
from api.model_calls import FLEET, Payer
from api.task_config import configured_model, configured_shape
from core.batch import BatchEventCounts, BatchResult
from core.routing import Choice, TaskShape, resolve
from tasks.runtime.lifecycle import claim_guard

logger = logging.getLogger(__name__)


def _record_batch_ids(task_id: int, batch_ids: list[str], conn: Any = None) -> None:
    """Appends batch ids to the task payload, skipping any already recorded.

    Both the event hook (as each batch is accepted) and the park (as a safety
    net for a submit that ran without a hook) record the same ids, so this has
    to be idempotent or a two-wave submit parks with [b1, b2, b1, b2] and
    collection downloads every output file twice.
    """
    sql = """
        UPDATE tasks SET payload = jsonb_set(
            COALESCE(payload, '{}'::jsonb), '{batch_ids}',
            COALESCE(payload->'batch_ids', '[]'::jsonb) || COALESCE((
                SELECT jsonb_agg(v)
                FROM jsonb_array_elements(%(ids)s::jsonb) AS v
                WHERE NOT COALESCE(payload->'batch_ids', '[]'::jsonb) ? (v #>> '{}')
            ), '[]'::jsonb))
        WHERE id = %(tid)s
        """
    params = {"ids": db.jsonb(batch_ids), "tid": task_id}
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.execute(sql, params)


class AwaitingBatch(Exception):
    """Raised by a handler that has submitted provider batches and has nothing
    left to do until they finish.

    A batch lives entirely provider-side while it queues, so a worker that sits
    polling one is doing no work and cannot be used for anything else. Raising
    this parks the task and frees the slot; poll_batches resumes it once every
    batch reached a terminal state, or gives up once the provider has breached
    its own completion window.
    """


def park_awaiting_batch(task_id: int, batch_ids: list[str]) -> bool:
    """Records the batches a task is waiting on and releases the worker.

    The ids go in the payload so a resumed run reattaches to work already paid
    for instead of resubmitting - the same guarantee pending_batch_ids gave a
    crashed worker, now used as the normal path rather than as recovery.

    Returns False when the task was no longer claimable (cancelled, or reaped
    out from under us). That matters: the batches are already submitted and
    billed, so losing their ids here would orphan paid work with nothing left
    pointing at it. The caller must surface that rather than park silently.

    The ids are recorded before the status flips, and unconditionally - even
    when this worker has lost the claim - because an id that reaches no row is
    paid work nothing points at. Only the park itself is refused.
    """
    owned, owned_params = claim_guard(task_id)
    with db.pool.connection() as conn:
        _record_batch_ids(task_id, batch_ids, conn=conn)
        result = conn.execute(
            f"""
            UPDATE tasks SET status = 'awaiting_batch', started_at = NULL,
                last_heartbeat = NULL
            WHERE id = %(tid)s AND status = 'running'{owned}
            """,
            {"tid": task_id, **owned_params},
        )
        parked = bool(result.rowcount)
    if parked:
        events.publish_task(task_id)
    return parked


@dataclasses.dataclass(frozen=True)
class BatchProvenance:
    model: str | None


def _batch_metadata(task_id: int, batch_ids: list[str]) -> dict[str, dict]:
    return {
        row["provider_batch_id"]: row
        for row in db.query(
            "SELECT provider_batch_id, model FROM ai_batches "
            "WHERE task_id = %s AND provider_batch_id = ANY(%s)",
            (task_id, batch_ids),
        )
    }


def batch_event_hook(
    task_id: int,
    purpose: str,
    model: str | None,
    *,
    payer: Payer = FLEET,
):
    """Register provider progress and who pays for the batch.

    The batch row records the payer, so the receipt checkpoint can write each
    item to the call ledger (api.model_calls) with it. Cost is the ledger's,
    one row per request: nothing here totals or prices a batch.
    """

    resumed_ids = set(pending_batch_ids(task_id))
    metadata = _batch_metadata(task_id, list(resumed_ids)) if resumed_ids else {}

    def record_event(batch_id: str, status: str, counts: BatchEventCounts) -> None:
        persisted = metadata.get(batch_id, {})
        event_model = persisted.get("model") if batch_id in resumed_ids else model
        db.execute(
            """
            INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model,
                                    requests, completed, failed_count, status, est_tokens,
                                    payer, payer_id)
            VALUES (%(bid)s, %(tid)s, %(purpose)s, %(model)s,
                    %(requests)s, %(completed)s, %(failed)s, %(status)s, %(est)s,
                    %(payer)s, %(payer_id)s)
            ON CONFLICT (provider_batch_id) DO UPDATE SET
                -- A batch submitted before payers were recorded takes the
                -- payer of the task collecting it, which is the task that
                -- submitted it.
                payer = COALESCE(ai_batches.payer, EXCLUDED.payer),
                payer_id = CASE WHEN ai_batches.payer IS NULL THEN EXCLUDED.payer_id
                                ELSE ai_batches.payer_id END,
                requests = GREATEST(ai_batches.requests, EXCLUDED.requests),
                completed = EXCLUDED.completed,
                failed_count = EXCLUDED.failed_count,
                status = EXCLUDED.status,
                est_tokens = GREATEST(ai_batches.est_tokens, EXCLUDED.est_tokens),
                updated_at = now(),
                completed_at = CASE WHEN EXCLUDED.status IN
                    ('completed', 'failed', 'expired', 'cancelled')
                    THEN COALESCE(ai_batches.completed_at, now()) ELSE NULL END
            """,
            {
                "bid": batch_id,
                "tid": task_id,
                "purpose": purpose,
                "model": event_model,
                "requests": counts.get("requests", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "status": status,
                "est": counts.get("est_tokens", 0),
                "payer": payer.kind,
                "payer_id": payer.id,
            },
        )
        _record_batch_ids(task_id, [batch_id])

    def on_event(batch_id: str, status: str, counts: BatchEventCounts) -> None:
        with db.transaction():
            record_event(batch_id, status, counts)
        events.publish_task(task_id)

    return on_event


async def run_batched(
    task_id: int,
    shape: TaskShape,
    specs: list,
    *,
    payer: Payer = FLEET,
    allow_configured_override: bool = True,
) -> tuple[list[BatchResult], Choice | BatchProvenance]:
    """Submit using current routing, or collect using persisted batch provenance.

    Collection never resolves current routing or applies a new-spend gate.
    A result's model comes from its own batch row; missing metadata remains
    unknown. User-charged callers book their results instead of the fleet hook.
    """
    purpose = shape.purpose
    existing = pending_batch_ids(task_id)
    if has_batch_work(task_id):
        metadata = _batch_metadata(task_id, existing)
        models = {metadata.get(batch_id, {}).get("model") for batch_id in existing}
        provenance = BatchProvenance(next(iter(models)) if len(models) == 1 else None)
        hook = batch_event_hook(task_id, purpose, None, payer=payer)
        results = await collect_pending(task_id, hook)
        return results, provenance
    specs = snapshot_specs(task_id, specs)
    shape = configured_shape(shape)
    chosen = resolve(
        shape, override=configured_model(purpose) if allow_configured_override else None
    )
    if payer == FLEET:
        # Only when about to SUBMIT. A resuming task is collecting work the
        # provider has already been paid for, and refusing that would discard
        # it - the ceiling exists to stop new spend, not to strand old.
        #
        # Priced with what THIS submission will cost, not just what has been
        # spent. A ceiling checked against history alone lets one large batch
        # cross it in a single step and refuses on the following cycle, after
        # the money is gone - which is the failure the ceiling exists to
        # prevent, committed by the ceiling.
        per_call = chosen.est_cost_usd or Decimal(0)
        budget.check_fleet_budget(per_call * len(specs))
    logger.info(f"Task {task_id}: {purpose} on {chosen.model} - {chosen.reason}")
    hook = batch_event_hook(task_id, purpose, chosen.model, payer=payer)
    results = await submit_or_collect(
        task_id,
        specs,
        chosen.model,
        # An override may reject the shape's default effort. Use the effort
        # resolved for the model actually being submitted. An override once
        # sent the shape's "none" to a model that rejected it: requirements
        # received HTTP 400 for 21,525 lines on 2026-09-04 and 112 on 09-05.
        str(chosen.params.get("reasoning_effort") or shape.resolved_effort() or ""),
        shape.max_output_tokens,
        hook,
    )
    return results, chosen


async def submit_or_collect(
    task_id: int,
    specs: list,
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    hook,
) -> list[BatchResult]:
    """Submit frozen requests and park, or return persisted results for consumption.

    A retry collects existing work before considering new submission. Request
    snapshots alone remain retryable when no provider batch was accepted.
    """
    from core.batch import submit_responses_batches

    existing = pending_batch_ids(task_id)
    if has_batch_work(task_id):
        logger.info(f"Task {task_id}: collecting {len(existing)} batch(es)")
        return await collect_pending(task_id, hook)

    specs = snapshot_specs(task_id, specs)
    if not specs:
        return []
    ids = await submit_responses_batches(
        specs, model, reasoning_effort, max_output_tokens, on_event=hook
    )
    if not ids:
        # Nothing was accepted by the provider; fail normally so the usual
        # retry path applies rather than parking forever.
        raise RuntimeError("no batches were accepted by the provider")
    if not park_awaiting_batch(task_id, ids):
        # ai_batches still records them (the event hook fired on submission),
        # so they are recoverable by hand - but nothing will collect them
        # automatically, which is worth failing loudly over.
        raise RuntimeError(
            f"submitted {len(ids)} batch(es) but task {task_id} was no longer "
            f"claimable; ids recorded in ai_batches: {', '.join(ids)}"
        )
    raise AwaitingBatch()


def pending_batch_ids(task_id: int) -> list[str]:
    row = db.query_one("SELECT payload->'batch_ids' AS ids FROM tasks WHERE id = %s", (task_id,))
    return list(row["ids"]) if row and row["ids"] else []


def resume_parked(task_id: int) -> None:
    """Back to pending for the claim that collects. That claim is not a retry,
    so it is counted in batch_resumes and the retry budget gives it back."""
    db.execute(
        "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL, "
        "batch_resumes = batch_resumes + 1 "
        "WHERE id = %s AND status = 'awaiting_batch'",
        (task_id,),
    )
    events.publish_task(task_id)


def has_batch_work(task_id: int) -> bool:
    row = db.query_one(
        "SELECT payload->'batch_ids' AS ids, "
        "payload->'batch_collection_checkpointed' AS collected FROM tasks WHERE id=%s",
        (task_id,),
    )
    return bool(row and (row["ids"] or row["collected"] is True)) or batch_results.has_results(
        task_id
    )


async def collect_pending(task_id: int, hook) -> list[BatchResult]:
    """Checkpoint paid responses before clearing provider IDs; replay until consumed."""
    from core.batch import collect_finished_batches

    existing = pending_batch_ids(task_id)
    if existing:
        metadata = _batch_metadata(task_id, existing)
        results, unfinished = await collect_finished_batches(existing, hook)
        for result in results:
            result.model = (
                metadata.get(result.batch_id, {}).get("model") if result.batch_id else None
            )
        batch_results.checkpoint(task_id, results, unfinished)
    return batch_results.unconsumed(task_id)


def repark_if_unfinished(task_id: int) -> bool:
    """After a handler returns: if its payload still names batches, the run
    collected only part of its work and the task waits on the rest. True when
    it was parked again; the caller must then not finish it."""
    remaining = pending_batch_ids(task_id)
    if not remaining:
        return False
    return park_awaiting_batch(task_id, remaining)
