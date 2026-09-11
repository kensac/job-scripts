"""A task's life: the claim that proves it is still ours, its progress, its
end, and the parent a chunk reports to.

Every write here is guarded by the claim, because `status` alone cannot say
who owns a row: the reaper requeues a stale task and another worker claims it
back, and both states look identical to the worker that lost it.
"""

from __future__ import annotations

import datetime
import logging
from contextvars import ContextVar
from typing import Any, LiteralString, NamedTuple

from api import db, events
from tasks.board import demote_closed, materialize_passing

logger = logging.getLogger(__name__)


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


def claim_guard(task_id: int) -> tuple[LiteralString, dict[str, Any]]:
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


class Deferred(Exception):
    """The task goes back to pending, unclaimed until not_before, with no
    attempt spent: the host's slot for this address is not open yet."""

    def __init__(self, not_before: datetime.datetime) -> None:
        super().__init__(f"deferred until {not_before:%H:%M:%S}")
        self.not_before = not_before


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
    owned, owned_params = claim_guard(task_id)
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
    owned, owned_params = claim_guard(task_id)
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
