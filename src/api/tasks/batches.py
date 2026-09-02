"""Resumes tasks parked on provider batches."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.tasks.runtime import _resume_parked, _set_progress

logger = logging.getLogger("jobtracker_worker")


def _record_progress(progress: dict[str, Any]) -> None:
    """Write back what the provider just told us.

    The poll already knows every batch's live status - it fetches it each
    minute to decide whether to resume - and until now it used the answer for
    one boolean and discarded it. So `ai_batches.status` kept whatever the
    submitting task last wrote and stayed there for the whole life of the
    batch: a batch reported `validating` for two hours while the provider had
    it `in_progress` at 446 of 501 requests.

    That makes the column lie in both directions. Work in flight reads as
    stuck, and a batch that genuinely stalls looks exactly like one that is
    fine, so the admin view cannot tell them apart. Nothing here costs a
    request - the status call already happened.

    `completed_at` mirrors the submit-time hook rather than inventing a second
    rule for when a batch finished.
    """
    for batch_id, state in progress.items():
        if not state.status:
            continue
        db.execute(
            """
            UPDATE ai_batches SET status = %(status)s, updated_at = now(),
                requests = GREATEST(requests, %(total)s),
                completed = GREATEST(completed, %(completed)s),
                failed_count = GREATEST(failed_count, %(failed)s),
                completed_at = CASE WHEN %(status)s IN
                    ('completed', 'failed', 'expired', 'cancelled')
                    THEN COALESCE(completed_at, now()) ELSE NULL END
            WHERE provider_batch_id = %(bid)s
            """,
            {
                "status": state.status,
                # GREATEST because a provider that briefly reports fewer
                # completed than we already recorded should not walk the
                # number backwards - progress here is monotonic by nature.
                "total": state.total,
                "completed": state.completed,
                "failed": state.failed,
                "bid": batch_id,
            },
        )


async def handle_poll_batches(task_id: int, payload: dict[str, Any]) -> None:
    """Resumes tasks whose provider batches have finished.

    This is the half that makes parking safe: without it a parked task would
    wait forever. It only asks the provider for status - it never downloads
    output - so checking every in-flight batch costs about as much as checking
    one, and the handler that resumes does the actual collection.
    """
    from core.batch import batch_progress, completion_window_seconds, is_terminal

    parked = db.query(
        "SELECT id, kind, payload FROM tasks WHERE status = 'awaiting_batch' ORDER BY id"
    )
    if not parked:
        _set_progress(task_id, 0, 0, "nothing awaiting batches")
        return

    # The provider guarantees a terminal state inside the window we asked for,
    # so that window IS the deadline - no invented timeout, and it moves
    # automatically if BATCH_COMPLETION_WINDOW ever changes.
    window = completion_window_seconds()
    resumed = expired = 0
    for t in parked:
        ids = list((t["payload"] or {}).get("batch_ids") or [])
        if not ids:
            # Parked with nothing to wait for: resume rather than strand it.
            _resume_parked(t["id"])
            resumed += 1
            continue
        progress = await batch_progress(ids)
        _record_progress(progress)
        states = {k: v.status for k, v in progress.items()}
        # A batch we cannot read a status for is treated as unfinished, so a
        # transient provider error delays a resume instead of dropping results.
        if all(is_terminal(states.get(b, "")) for b in ids):
            _resume_parked(t["id"])
            resumed += 1
            continue
        overdue = db.query_one(
            "SELECT 1 FROM ai_batches WHERE provider_batch_id = ANY(%s) "
            "AND submitted_at < now() - make_interval(secs => %s) LIMIT 1",
            (ids, window),
        )
        if overdue:
            # Past the provider's own guarantee. Resume anyway: collection
            # records whatever did land and leaves the rest to the next sweep,
            # which is strictly better than failing and discarding paid work.
            logger.warning(
                f"Task {t['id']} batches exceeded the {window}s completion window; collecting"
            )
            _resume_parked(t["id"])
            expired += 1
    _set_progress(
        task_id,
        resumed + expired,
        len(parked),
        f"{resumed} resumed, {expired} past the completion window",
    )
