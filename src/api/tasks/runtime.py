"""Task-execution primitives shared by every handler.

Sits below the handlers so a handler can import it without pulling in the
worker loop, while the loop imports handlers for its registry. That ordering
is what keeps the two from forming a cycle.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from api import ai, budget, db, events, metrics
from api.budget import Entitlement
from api.tasks.board import _demote_closed, _materialize_passing

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
    """
    with db.pool.connection() as conn:
        result = conn.execute(
            """
            UPDATE tasks SET status = 'awaiting_batch', started_at = NULL,
                last_heartbeat = NULL,
                payload = jsonb_set(
                    COALESCE(payload, '{}'::jsonb), '{batch_ids}',
                    COALESCE(payload->'batch_ids', '[]'::jsonb) || %(ids)s::jsonb)
            WHERE id = %(tid)s AND status IN ('running', 'pending')
            """,
            {"ids": db.jsonb(batch_ids), "tid": task_id},
        )
        parked = bool(result.rowcount)
    if parked:
        events.publish_task(task_id)
    return parked


def _finish(task_id: int, status: str, error: str | None = None) -> None:
    # Only running tasks can be finished; an admin 'cancelled' status sticks.
    db.execute(
        "UPDATE tasks SET status = %s, error = %s, finished_at = now() "
        "WHERE id = %s AND status = 'running'",
        (status, error[:500] if error else None, task_id),
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
    db.execute(
        "UPDATE tasks SET progress = %s, last_heartbeat = now() WHERE id = %s",
        (db.jsonb({"done": done, "total": total, "label": label}), task_id),
    )
    events.publish_task(task_id)


def _batch_event_hook(task_id: int, purpose: str, model: str):
    """Registers every provider batch in ai_batches as it progresses, and
    stores submitted batch ids on the task payload so a requeued attempt
    reattaches instead of resubmitting (double spend + orphaned results)."""

    def on_event(batch_id: str, status: str, counts: dict[str, int]) -> None:
        if "input_tokens" in counts or "output_tokens" in counts:
            # Terminal usage report: real token totals -> half-price batch cost.
            inp = counts.get("input_tokens", 0)
            out = counts.get("output_tokens", 0)
            prices = ai.PRICES_PER_MTOK.get(model)
            cost = round((inp * prices[0] + out * prices[1]) / 2_000_000, 6) if prices else None
            db.execute(
                "UPDATE ai_batches SET input_tokens = input_tokens + %s, "
                "output_tokens = output_tokens + %s, "
                "est_cost_usd = COALESCE(est_cost_usd, 0) + COALESCE(%s, 0), "
                "updated_at = now() WHERE provider_batch_id = %s",
                (inp, out, cost, batch_id),
            )
            events.publish_task(task_id)
            return
        db.execute(
            """
            INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model,
                                    requests, completed, failed_count, status, est_tokens)
            VALUES (%(bid)s, %(tid)s, %(purpose)s, %(model)s,
                    %(requests)s, %(completed)s, %(failed)s, %(status)s, %(est)s)
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
            },
        )
        db.execute(
            """
            UPDATE tasks SET payload = jsonb_set(payload, '{batch_ids}',
                COALESCE(payload->'batch_ids', '[]'::jsonb) ||
                CASE WHEN payload->'batch_ids' ? %(bid)s THEN '[]'::jsonb
                     ELSE to_jsonb(%(bid)s::text) END)
            WHERE id = %(tid)s
            """,
            {"bid": batch_id, "tid": task_id},
        )
        events.publish_task(task_id)

    return on_event


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
