"""Task-execution primitives shared by every handler.

Sits below the handlers so a handler can import it without pulling in the
worker loop, while the loop imports handlers for its registry. That ordering
is what keeps the two from forming a cycle.
"""

from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from typing import Any, LiteralString, NamedTuple

from api import ai, budget, db, events, metrics
from api.budget import Entitlement
from api.tasks.board import _demote_closed, _materialize_passing
from core import pricing
from core.prompts import PROMPT_SAMPLE_SIZE, prompt_hash
from core.routing import Choice, TaskShape, resolve

logger = logging.getLogger("jobtracker_worker")


MAX_CONCURRENCY = int(os.environ.get("JOBTRACKER_MAX_CONCURRENCY", "6"))


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


def enqueue(kind: str, payload: dict[str, Any], dedupe_key: str | None = None) -> int | None:
    """Insert a task; with a dedupe_key, at most one task per key ever exists,
    so every fleet worker can race to enqueue and exactly one wins.

    parent_id is mirrored out of the payload into its own column: it is the
    only payload field that gets queried, and no index can serve
    payload->>'parent_id'. It stays in the payload too so a chunk handler
    reading its own payload is unchanged."""
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, dedupe_key, parent_id) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (kind, db.jsonb(payload), dedupe_key, payload.get("parent_id")),
    )
    if row:
        events.publish_task(row["id"])
    return row["id"] if row else None


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
    for instead of resubmitting - the same guarantee _pending_batch_ids gave a
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


def _finish(task_id: int, status: str, error: str | None = None) -> None:
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


def _cancelled(task_id: int) -> bool:
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    return not row or row["status"] != "running"


def _parent_cancelled(parent_id: int) -> bool:
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (parent_id,))
    return not row or row["status"] == "cancelled"


def _update_parent_progress(parent_id: int) -> None:
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


def _maybe_finalize_parent(parent_id: int) -> None:
    _update_parent_progress(parent_id)
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
            _demote_closed()
        except Exception:
            logger.exception("demotion failed")
    elif parent and (parent["payload"] or {}).get("user_id"):
        try:
            _materialize_passing(parent["payload"]["user_id"])
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


def _reconcile_chunks() -> None:
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
        _maybe_finalize_parent(r["id"])


def _set_progress(task_id: int, done: int, total: int, label: str) -> None:
    # The heartbeat rides along with progress, so this write has to respect the
    # claim too: a worker that lost the task would otherwise keep proving the
    # liveness of the run that replaced it, and the reaper would never see it.
    owned, owned_params = _owned(task_id)
    db.execute(
        f"UPDATE tasks SET progress = %(progress)s, last_heartbeat = now() "
        f"WHERE id = %(tid)s{owned}",
        {
            "progress": db.jsonb({"done": done, "total": total, "label": label}),
            "tid": task_id,
            **owned_params,
        },
    )
    events.publish_task(task_id)


def _batch_event_hook(
    task_id: int,
    purpose: str,
    model: str,
    prompt_id: int | None = None,
    *,
    charged_to_user: bool = False,
):
    """Registers every provider batch in ai_batches as it progresses, and
    stores submitted batch ids on the task payload so a requeued attempt
    reattaches instead of resubmitting (double spend + orphaned results).

    `charged_to_user` says the caller already books these tokens against a
    person, so this must not book them again against the fleet. Filter runs are
    the case: the batched sweep records every result with budget.record_usage(user_id)
    and this hook was recording the same tokens a second time with user_id NULL.
    Two rows for one call made /admin/spend read filter work at double its cost
    and let one person's usage consume the fleet's weekly ceiling.
    """

    def on_event(batch_id: str, status: str, counts: dict[str, int]) -> None:
        if "input_tokens" in counts or "output_tokens" in counts:
            # Terminal usage report: real token totals -> half-price batch cost.
            inp = counts.get("input_tokens", 0)
            out = counts.get("output_tokens", 0)
            est = pricing.estimate_cost_usd(model, inp, out, batched=True)
            cost = round(float(est), 6) if est is not None else None
            db.execute(
                "UPDATE ai_batches SET input_tokens = input_tokens + %s, "
                "output_tokens = output_tokens + %s, "
                "est_cost_usd = COALESCE(est_cost_usd, 0) + COALESCE(%s, 0), "
                "updated_at = now() WHERE provider_batch_id = %s",
                (inp, out, cost, batch_id),
            )
            # The same numbers into the spend ledger. Every batched caller
            # passes through here and already names a purpose, so a new AI
            # caller shows up in analytics without anyone wiring it - the hook
            # cannot be used without a purpose, and that is all the grouping
            # needs.
            if not charged_to_user:
                budget.record_fleet_usage(purpose, model, inp, out, batched=True)
            events.publish_task(task_id)
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
                "model": model,
                "requests": counts.get("requests", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "status": status,
                "est": counts.get("est_tokens", 0),
                "prompt_id": prompt_id,
            },
        )
        _record_batch_ids(task_id, [batch_id])
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


def _record_prompt_samples(prompt_id: int | None, results: dict[str, Any]) -> None:
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
        rows = [
            (prompt_id, custom_id, res.text, res.error)
            for custom_id, res in list(results.items())[:room]
        ]
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
) -> tuple[dict[str, Any], Choice]:
    """The one way a scheduled handler runs a batch.

    Every batch call site had the same ten lines: resolve, build a hook,
    check for in-flight ids, reattach or submit. Four copies, and they had
    already drifted - one passed `SHAPE.effort or "low"`, another
    `SHAPE.resolved_effort() or A_CONSTANT`, so the same declaration produced
    different requests depending on which file you were in.

    Taking the shape rather than a model, an effort and a token cap removes the
    chance to disagree with it. The shape is the single declaration of what the
    work needs; unpacking it at four call sites is what let them diverge.

    The purpose every ledger groups by comes from the SHAPE rather than beside
    it, so a handler cannot name one purpose while running another's shape -
    and so the key that configures a task is the same key that reports it.
    A handler cannot run a batch without its cost, tokens and model landing in
    analytics.
    Anything recorded here in future - prompt identity, output samples - lands
    for every caller at once rather than being added to four files and missed
    in a fifth.
    """
    purpose = shape.purpose
    # Before anything is submitted: a batch the provider has accepted is
    # billable whether or not this system still wants it.
    budget.check_fleet_budget()
    chosen = resolve(shape, override=configured_model(purpose))
    logger.info(f"Task {task_id}: {purpose} on {chosen.model} - {chosen.reason}")
    # Every spec in a sweep carries the same instructions - they are module
    # constants - so the first is the prompt for the batch. Recorded before
    # submitting, so a sweep that dies mid-flight still says what it asked.
    prompt_id = _record_prompt(purpose, specs[0].instructions) if specs else None
    hook = _batch_event_hook(task_id, purpose, chosen.model, prompt_id=prompt_id)
    existing = _pending_batch_ids(task_id)
    if existing:
        from core.batch import collect_batches

        logger.info(f"Task {task_id}: reattaching to {len(existing)} in-flight batch(es)")
        results = await collect_batches(existing, hook)
    else:
        results = await submit_or_collect(
            task_id,
            specs,
            chosen.model,
            # The shape's own answer, not the call site's. A model that rejects
            # the value is not eligible, so resolve() has already refused
            # rather than letting a batch fail whole on a bad parameter.
            shape.resolved_effort() or "",
            shape.max_output_tokens,
            hook,
        )
    # One exit, so the reattach path cannot quietly skip what the submit path
    # records. A requeued sweep is the case that would lose its provenance,
    # and it is the harder one to notice missing.
    _record_prompt_samples(prompt_id, results)
    return results, chosen


async def submit_or_collect(
    task_id: int,
    specs: list,
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    hook,
) -> dict[str, Any]:
    """The one way a scheduled handler runs a batch.

    First call submits and raises AwaitingBatch, freeing the worker. When
    poll_batches sees every batch reach a terminal state it flips the task back
    to pending; the handler then re-runs, lands here again, finds the ids in
    its payload and collects the results without resubmitting.

    Callers must build their specs before calling and be safe to re-run from
    the top, which they already are - every batched sweep was written to be
    idempotent by re-sweep.
    """
    from core.batch import collect_batches, submit_responses_batches

    existing = _pending_batch_ids(task_id)
    if existing:
        logger.info(f"Task {task_id}: collecting {len(existing)} finished batch(es)")
        return await collect_batches(existing, hook)

    if not specs:
        return {}
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


def _pending_batch_ids(task_id: int) -> list[str]:
    row = db.query_one("SELECT payload->'batch_ids' AS ids FROM tasks WHERE id = %s", (task_id,))
    return list(row["ids"]) if row and row["ids"] else []


def _resume_parked(task_id: int) -> None:
    db.execute(
        "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL "
        "WHERE id = %s AND status = 'awaiting_batch'",
        (task_id,),
    )
    events.publish_task(task_id)


def _load_config(user_id: int) -> tuple[Entitlement, ai.AIConfig]:
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
    return ent, budget.resolve_ai_config(user_id, ent)
