"""Resumes tasks parked on provider batches."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.tasks.runtime import _resume_parked, _set_progress

logger = logging.getLogger("jobtracker_worker")


async def handle_poll_batches(task_id: int, payload: dict[str, Any]) -> None:
    """Resumes tasks whose provider batches have finished.

    This is the half that makes parking safe: without it a parked task would
    wait forever. It only asks the provider for status - it never downloads
    output - so checking every in-flight batch costs about as much as checking
    one, and the handler that resumes does the actual collection.
    """
    from core.batch import batch_states, completion_window_seconds, is_terminal

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
        states = await batch_states(ids)
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
