"""Provider batches: submitting one, parking the task on it, collecting it,
and recording what it cost and what it was asked.

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
from api.task_config import configured_model
from core import pricing
from core.batch import BatchEventCounts, BatchResult
from core.prompts import PROMPT_SAMPLE_SIZE, prompt_hash
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
            "SELECT provider_batch_id, model, prompt_id FROM ai_batches "
            "WHERE task_id = %s AND provider_batch_id = ANY(%s)",
            (task_id, batch_ids),
        )
    }


def batch_event_hook(
    task_id: int,
    purpose: str,
    model: str | None,
    prompt_id: int | None = None,
    *,
    charged_to_user: bool = False,
):
    """Register provider progress and atomically record fleet usage.

    User-charged callers account for individual receipts instead; the hook
    must not book those same calls against the fleet.
    """

    resumed_ids = set(pending_batch_ids(task_id))
    metadata = _batch_metadata(task_id, list(resumed_ids)) if resumed_ids else {}

    def record_event(batch_id: str, status: str, counts: BatchEventCounts) -> None:
        persisted = metadata.get(batch_id, {})
        event_model = persisted.get("model") if batch_id in resumed_ids else model
        event_prompt_id = persisted.get("prompt_id") if batch_id in resumed_ids else prompt_id
        if "input_tokens" in counts or "output_tokens" in counts:
            # Keep request boundaries: pricing tiers apply to individual prompts.
            inp = counts.get("input_tokens", 0)
            out = counts.get("output_tokens", 0)
            cached = counts.get("cached_tokens", 0)
            usage = counts.get("request_usage")
            est = pricing.estimate_usage_cost_usd(
                event_model, inp, out, cached_tokens=cached, batched=True, requests=usage
            )
            cost = round(float(est), 6) if est is not None else None
            # Provider totals are snapshots. Recollecting unchanged totals
            # must not append another ledger entry. The pre-checkpoint audit
            # found 92 batched tasks at attempts=2 (ordinary park/resume), none
            # at attempts=3 (collect/fail/recollect). Double booking was then
            # a reachable risk, not an observed incident; retain the distinction.
            written = db.execute_count(
                "UPDATE ai_batches SET input_tokens = %s, output_tokens = %s, "
                "est_cost_usd = %s, updated_at = now() "
                "WHERE provider_batch_id = %s "
                "AND (input_tokens, output_tokens) IS DISTINCT FROM (%s, %s)",
                (inp, out, cost, batch_id, inp, out),
            )
            if not written:
                # Already recorded with these exact totals: this is a repeat
                # collection of a batch that has not changed, so the ledger
                # must not gain a second row for it either.
                return
            # The same numbers into the spend ledger. Every batched caller
            # passes through here and already names a purpose, so a new AI
            # caller shows up in analytics without anyone wiring it - the hook
            # cannot be used without a purpose, and that is all the grouping
            # needs.
            if not charged_to_user:
                budget.record_fleet_usage(
                    purpose,
                    event_model,
                    inp,
                    out,
                    batched=True,
                    cached_tokens=cached,
                    request_usage=usage,
                )
            return
        db.execute(
            """
            INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model,
                                    requests, completed, failed_count, status, est_tokens,
                                    prompt_id)
            VALUES (%(bid)s, %(tid)s, %(purpose)s, %(model)s,
                    %(requests)s, %(completed)s, %(failed)s, %(status)s, %(est)s,
                    %(prompt_id)s)
            ON CONFLICT (provider_batch_id) DO UPDATE SET
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
                "prompt_id": event_prompt_id,
            },
        )
        _record_batch_ids(task_id, [batch_id])

    def on_event(batch_id: str, status: str, counts: BatchEventCounts) -> None:
        with db.transaction():
            record_event(batch_id, status, counts)
        events.publish_task(task_id)

    return on_event


def _record_prompt(purpose: str, instructions: str) -> int | None:
    """One row per distinct instruction text, and its id.

    Upsert rather than insert: the same prompt runs every cycle, and the row
    that matters is the first sighting plus the fact that it is still in use.
    Returns None rather than raising if the write fails - provenance is
    reporting, and losing it must never take down a sweep that is otherwise
    ready to spend money correctly.
    """
    try:
        row = db.query_one(
            """
            INSERT INTO ai_prompts (prompt_hash, purpose, instructions, batches)
            VALUES (%(hash)s, %(purpose)s, %(instructions)s, 1)
            ON CONFLICT (prompt_hash) DO UPDATE
                SET last_seen_at = now(), batches = ai_prompts.batches + 1
            RETURNING id
            """,
            {
                "hash": prompt_hash(instructions),
                "purpose": purpose,
                "instructions": instructions,
            },
        )
        return row["id"] if row else None
    except Exception:
        logger.warning(f"could not record prompt for {purpose}", exc_info=True)
        return None


def _record_prompt_samples(prompt_id: int | None, results: list[BatchResult]) -> None:
    """Up to PROMPT_SAMPLE_SIZE outputs per prompt version, never more.

    The cap is per prompt rather than per sweep, so a prompt running hourly for
    a year holds 100 rows and not 8,760. Counting first and inserting the
    remainder is a race between two workers finishing batches at once, and the
    race is harmless: the loser overshoots the cap by a few rows, which costs
    bytes rather than correctness. A unique constraint would turn that into a
    failed sweep.

    Errored lines are sampled too, with their error instead of an output. A
    prompt change that starts producing unparseable JSON is exactly the change
    worth seeing, and it leaves no output to record.
    """
    if prompt_id is None or not results:
        return
    try:
        held = db.query_one(
            "SELECT COUNT(*) AS n FROM ai_prompt_samples WHERE prompt_id = %s", (prompt_id,)
        )
        room = PROMPT_SAMPLE_SIZE - ((held or {}).get("n") or 0)
        if room <= 0:
            return
        rows = [(prompt_id, res.custom_id, res.text, res.error) for res in results[:room]]
        if rows:
            with db.pool.connection() as conn:
                conn.cursor().executemany(
                    "INSERT INTO ai_prompt_samples (prompt_id, custom_id, output, error) "
                    "VALUES (%s, %s, %s, %s)",
                    rows,
                )
    except Exception:
        logger.warning("could not record prompt samples", exc_info=True)


async def run_batched(
    task_id: int,
    shape: TaskShape,
    specs: list,
    *,
    charged_to_user: bool = False,
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
        hook = batch_event_hook(task_id, purpose, None, charged_to_user=charged_to_user)
        results = await collect_pending(task_id, hook)
        return results, provenance
    specs = snapshot_specs(task_id, specs)
    chosen = resolve(shape, override=configured_model(purpose))
    if not charged_to_user:
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
    # Every spec in a sweep carries the same instructions - they are module
    # constants - so the first is the prompt for the batch. Recorded before
    # submitting, so a sweep that dies mid-flight still says what it asked.
    prompt_id = _record_prompt(purpose, specs[0].instructions) if specs else None
    hook = batch_event_hook(
        task_id, purpose, chosen.model, prompt_id=prompt_id, charged_to_user=charged_to_user
    )
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
    _record_prompt_samples(prompt_id, results)
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
    db.execute(
        "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL "
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
        samples: dict[int, list[BatchResult]] = {}
        for result in results:
            if result.batch_id and (
                prompt_id := metadata.get(result.batch_id, {}).get("prompt_id")
            ):
                samples.setdefault(prompt_id, []).append(result)
        for prompt_id, sampled in samples.items():
            _record_prompt_samples(prompt_id, sampled)
    return batch_results.unconsumed(task_id)


def repark_if_unfinished(task_id: int) -> bool:
    """After a handler returns: if its payload still names batches, the run
    collected only part of its work and the task waits on the rest. True when
    it was parked again; the caller must then not finish it."""
    remaining = pending_batch_ids(task_id)
    if not remaining:
        return False
    return park_awaiting_batch(task_id, remaining)
