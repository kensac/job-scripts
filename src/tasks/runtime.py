"""Task-execution primitives shared by every handler.

Sits below the handlers so a handler can import it without pulling in the
worker loop, while the loop imports handlers for its registry. That ordering
is what keeps the two from forming a cycle.

The names here are public because they are used that way. They were spelled
with a leading underscore while sixteen modules imported set_progress and
eleven names in total crossed a module boundary, so the underscore was a claim
about the interface that the callers falsified. A handler reads this module to
find out what it may call; that is easier when the answer is "the public names"
than when it is "the private ones, apparently".

Anything genuinely internal to this module keeps its underscore.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import time
from contextvars import ContextVar
from decimal import Decimal
from typing import Any, LiteralString, NamedTuple

from api import ai, budget, db, events, metrics
from api.ai import batch_results
from api.ai.batch_results import consume_result as consume_result
from api.ai.batch_results import snapshot_specs as snapshot_specs
from api.budget import Entitlement
from api.queue import INGEST_INTERVAL_MINUTES, enqueue  # noqa: F401
from core import pricing
from core.batch import BatchEventCounts, BatchResult
from core.prompts import PROMPT_SAMPLE_SIZE, prompt_hash
from core.routing import Choice, TaskShape, resolve
from tasks.board import demote_closed, materialize_passing

logger = logging.getLogger("jobtracker_worker")


MAX_CONCURRENCY = int(os.environ.get("JOBTRACKER_MAX_CONCURRENCY", "6"))

# How often every active source is queued for ingest. The scheduler in


class AdaptiveLimiter:
    """AIMD concurrency control on a rolling throughput window: grow while the
    completion rate keeps improving, step down when it stalls or errors appear,
    halve on rate limits. Each host converges to its own ceiling."""

    def __init__(self, min_c: int = 1, max_c: int = MAX_CONCURRENCY, window: int = 8):
        self.limit = min(3, max_c)
        self.min_c = min_c
        self.max_c = max_c
        self.window = window
        self._count = 0
        self._errors = 0
        self._win_start = time.monotonic()
        self._prev_rate: float | None = None

    def record(self, error: bool = False, rate_limited: bool = False) -> None:
        if rate_limited:
            self.limit = max(self.min_c, self.limit // 2)
            self._reset()
            return
        if error:
            self._errors += 1
        self._count += 1
        if self._count < self.window:
            return
        elapsed = time.monotonic() - self._win_start
        rate = self._count / elapsed if elapsed > 0 else 0.0
        if self._errors:
            self.limit = max(self.min_c, self.limit - 1)
        elif self._prev_rate is None or rate >= self._prev_rate * 1.05:
            self.limit = min(self.max_c, self.limit + 1)
        elif rate < self._prev_rate * 0.9:
            self.limit = max(self.min_c, self.limit - 1)
        self._prev_rate = rate
        self._reset()

    def _reset(self) -> None:
        self._count = 0
        self._errors = 0
        self._win_start = time.monotonic()
        metrics.WORKER_CONCURRENCY.set(self.limit)


# In-flight jobs per worker inside a chunk (network time dominates, so calls
# overlap); the adaptive limiter tunes the actual level per host.


SCRAPE_CONCURRENCY = int(os.environ.get("JOBTRACKER_SCRAPE_CONCURRENCY", "2"))


# Filter runs shard into chunks of this many checks; the shared queue then
# load-balances by availability (fast workers simply claim more chunks).
CHUNK_SIZE = int(os.environ.get("JOBTRACKER_CHUNK_SIZE", "100"))


# Scheduled runs batch their AI calls through the OpenAI Batch API at half
# price; jobs in one batch chunk (content already cached, so no scraping).
BATCH_CHUNK_SIZE = int(os.environ.get("JOBTRACKER_BATCH_CHUNK_SIZE", "500"))


MAX_ATTEMPTS = 3


HEARTBEAT_TIMEOUT_MINUTES = 15


CHUNK_KINDS = ["run_filter_chunk", "reverify_chunk", "run_filter_batch_chunk"]


class TaskClaim(NamedTuple):
    """Proof that this worker still holds the task it claimed.

    `status` alone cannot express ownership: the reaper requeues a stale task
    to 'pending' and another worker claims it back to 'running', so both states
    look identical to the worker that lost it. attempts is incremented by the
    claim itself, which makes (worker, attempts) a generation stamp - any
    re-claim, by any host including this one, invalidates every claim before
    it.
    """

    task_id: int
    worker: str
    attempts: int


_current_claim: ContextVar[TaskClaim | None] = ContextVar("_current_claim", default=None)


def set_current_claim(claim: TaskClaim | None) -> None:
    """Called by the worker loop around a handler run. Nothing else claims
    tasks, so nothing else sets this."""
    _current_claim.set(claim)


def _owned(task_id: int) -> tuple[LiteralString, dict[str, Any]]:
    """The SQL tail and params that restrict a lifecycle write to the worker
    that still owns `task_id`.

    Outside the worker loop there is no claim to check - direct handler calls
    and tests - and the write stays as unrestricted as it was before.
    """
    claim = _current_claim.get()
    if claim is None or claim.task_id != task_id:
        return "", {}
    return " AND worker = %(_claim_worker)s AND attempts = %(_claim_attempts)s", {
        "_claim_worker": claim.worker,
        "_claim_attempts": claim.attempts,
    }


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


class Deferred(Exception):
    """The task goes back to pending, unclaimed until not_before, with no
    attempt spent: the host's slot for this address is not open yet."""

    def __init__(self, not_before: datetime.datetime) -> None:
        super().__init__(f"deferred until {not_before:%H:%M:%S}")
        self.not_before = not_before


class AwaitingBatch(Exception):
    """Raised by a handler that has submitted provider batches and has nothing
    left to do until they finish.

    A batch lives entirely provider-side while it queues, so a worker that sits
    polling one is doing no work and cannot be used for anything else. Raising
    this parks the task and frees the slot; poll_batches resumes it once every
    batch reached a terminal state, or gives up once the provider has breached
    its own completion window.
    """


def _park_awaiting_batch(task_id: int, batch_ids: list[str]) -> bool:
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
    owned, owned_params = _owned(task_id)
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


def finish(task_id: int, status: str, error: str | None = None) -> None:
    """Ends the task and closes out its batch lifecycle.

    Only running tasks can be finished; an admin 'cancelled' status sticks, and
    a worker that lost the claim must not finish the run that took it over.

    batch_ids are dropped here because this is the one point where the batches
    they name are provably spent. Leaving them behind lets a later re-run of
    the same row collect those outputs again and write verdicts from scraped
    text old enough to predate a closure. A retry *within* the run keeps them:
    that is the reattach path, and it is what stops paid work being resubmitted.
    """
    owned, owned_params = _owned(task_id)
    db.execute(
        f"""
        UPDATE tasks SET status = %(status)s, error = %(error)s, finished_at = now(),
            payload = COALESCE(payload, '{{}}'::jsonb) - 'batch_ids'
        WHERE id = %(tid)s AND status = 'running'{owned}
        """,
        {
            "status": status,
            "error": error[:500] if error else None,
            "tid": task_id,
            **owned_params,
        },
    )
    events.publish_task(task_id)


def cancelled(task_id: int) -> bool:
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    return not row or row["status"] != "running"


def parent_cancelled(parent_id: int) -> bool:
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (parent_id,))
    return not row or row["status"] == "cancelled"


def update_parent_progress(parent_id: int) -> None:
    agg = db.query_one(
        "SELECT COALESCE(SUM((progress->>'done')::int), 0) AS done FROM tasks "
        "WHERE kind = ANY(%s) AND parent_id = %s",
        (CHUNK_KINDS, parent_id),
    )
    db.execute(
        "UPDATE tasks SET progress = jsonb_set(COALESCE(progress, '{}'::jsonb), "
        "'{done}', to_jsonb(%s::int)), last_heartbeat = now() "
        "WHERE id = %s AND status = 'waiting'",
        (agg["done"] if agg else 0, parent_id),
    )
    events.publish_task(parent_id)


def maybe_finalize_parent(parent_id: int) -> None:
    update_parent_progress(parent_id)
    live = db.query_one(
        # awaiting_batch counts as live: a parked chunk has work in flight
        # at the provider, and finalizing the parent without it would publish
        # partial results as if they were complete.
        "SELECT COUNT(*) AS c FROM tasks WHERE kind = ANY(%s) "
        "AND parent_id = %s AND status IN ('pending', 'running', 'awaiting_batch')",
        (CHUNK_KINDS, parent_id),
    )
    if live and live["c"]:
        return
    failed = db.query_one(
        "SELECT COUNT(*) AS c FROM tasks WHERE kind = ANY(%s) "
        "AND parent_id = %s AND status = 'failed'",
        (CHUNK_KINDS, parent_id),
    )
    parent = db.query_one("SELECT kind, payload FROM tasks WHERE id = %s", (parent_id,))
    if parent and parent["kind"] == "reverify_open":
        try:
            demote_closed()
        except Exception:
            logger.exception("demotion failed")
    elif parent and (parent["payload"] or {}).get("user_id"):
        try:
            materialize_passing(parent["payload"]["user_id"])
        except Exception:
            logger.exception("materialize failed")
    n_failed = failed["c"] if failed else 0
    if n_failed:
        db.execute(
            "UPDATE tasks SET status = 'failed', error = %s, finished_at = now() "
            "WHERE id = %s AND status = 'waiting'",
            (f"{n_failed} chunk(s) failed", parent_id),
        )
    else:
        db.execute(
            "UPDATE tasks SET status = 'done', finished_at = now() "
            "WHERE id = %s AND status = 'waiting'",
            (parent_id,),
        )
    events.publish_task(parent_id)


def reconcile_chunks() -> None:
    db.execute(
        "UPDATE tasks SET status = 'cancelled', error = 'parent cancelled', finished_at = now() "
        "WHERE kind = ANY(%s) AND status = 'pending' "
        "AND parent_id IN (SELECT id FROM tasks WHERE status = 'cancelled')",
        (CHUNK_KINDS,),
    )
    for r in db.query(
        """
        SELECT id FROM tasks t WHERE t.status = 'waiting'
        AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.kind = ANY(%s)
            AND c.parent_id = t.id
            AND c.status IN ('pending', 'running', 'awaiting_batch'))
        """,
        (CHUNK_KINDS,),
    ):
        maybe_finalize_parent(r["id"])


def set_progress(
    task_id: int, done: int, total: int, label: str, extra: dict[str, Any] | None = None
) -> None:
    # `extra` is for counts a handler wants queryable afterwards (what an
    # ingest fetched, kept, cached, failed to fetch). The label is for a
    # person; the health detectors read the keys.
    #
    # The heartbeat rides along with progress, so this write has to respect the
    # claim too: a worker that lost the task would otherwise keep proving the
    # liveness of the run that replaced it, and the reaper would never see it.
    owned, owned_params = _owned(task_id)
    db.execute(
        # progress_at moves ONLY when the value differs. A handler that reports
        # the same numbers again has not advanced, and stamping it would make a
        # stalled handler indistinguishable from a working one - the same
        # mistake as a timer-driven heartbeat, one column along.
        f"UPDATE tasks SET progress = %(progress)s, last_heartbeat = now(), "
        f"    progress_at = CASE WHEN progress IS DISTINCT FROM %(progress)s "
        f"                       THEN now() ELSE progress_at END "
        f"WHERE id = %(tid)s{owned}",
        {
            "progress": db.jsonb({"done": done, "total": total, "label": label, **(extra or {})}),
            "tid": task_id,
            **owned_params,
        },
    )
    events.publish_task(task_id)


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


def configured_model(purpose: str) -> str | None:
    """The model a person has configured for this task, if any.

    Read here rather than inside resolve() because core does not import api and
    an override lives in the database. resolve() takes it as an argument and
    stays a pure function of the declaration plus one value, which is what lets
    the configuration screen ask "what would this do" without a write.

    Latest row wins, and the table is append-only, so the history a monthly
    review needs is the table itself rather than something reconstructed.
    Returns None on any failure: a configuration lookup that cannot be read
    must fall back to the call site's own judgment rather than stopping a sweep.
    """
    try:
        row = db.query_one(
            "SELECT model FROM task_model_overrides WHERE purpose = %s ORDER BY id DESC LIMIT 1",
            (purpose,),
        )
    except Exception:
        logger.warning(f"could not read the configured model for {purpose}", exc_info=True)
        return None
    return (row or {}).get("model") or None


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
    if not _park_awaiting_batch(task_id, ids):
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
    return _park_awaiting_batch(task_id, remaining)


def load_config(user_id: int, ignore_budget: bool = False) -> tuple[Entitlement, ai.AIConfig]:
    """The person's entitlement and model config for a task. With
    ignore_budget the shared weekly cap is lifted for this task only (an
    admin queued it that way): the spend is still recorded, the cap itself
    does not move (Kanishk, 2026-09-08: raising it and putting it back for
    one run was the wrong tool)."""
    user = db.query_one("SELECT id, sub, email, name, groups FROM users WHERE id = %s", (user_id,))
    if not user:
        raise LookupError("unknown user")
    from api.auth import AuthedUser

    authed = AuthedUser(
        id=user["id"],
        sub=user["sub"],
        email=user["email"] or "",
        name=user["name"] or "",
        groups=user["groups"] or [],
    )
    ent = budget.get_entitlement(authed)
    if ignore_budget and ent.owner_key:
        ent = dataclasses.replace(ent, weekly_token_budget=None)
    return ent, budget.resolve_ai_config(user_id, ent)
