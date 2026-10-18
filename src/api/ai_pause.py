"""The site-wide AI switch: `ai_paused` stops every paid model request.

Ingest, page fetches, availability and the boards' non-AI work keep running.
New spend is refused at the two places every paid request passes through:
`api.ai.parse` for live calls and `tasks.runtime.submit_or_collect` for
batches. Collecting a batch the provider already accepted is never refused,
because that work is paid for.

Turning the switch back on does not buy the pause's backlog. A posting first
seen during a pause, earlier than `ai_resume_backlog_days` before it ended,
is left out of verification, and every board, filter and extraction needs a
verification answer first, so it reaches none of them. It stays in the
catalog, and `verify_new` with a `urls` payload still verifies one by name.
The pauses are read from `app_config_changes`, the history the admin config
page writes, so there is no second record to keep in step.
"""

from __future__ import annotations

import datetime
from typing import Any

from api import db


class AIPaused(RuntimeError):
    """A paid model request while `ai_paused` is on."""

    def __init__(self) -> None:
        super().__init__("AI is paused by an administrator (ai_paused)")


def paused() -> bool:
    return bool(db.get_config("ai_paused"))


def check() -> None:
    if paused():
        raise AIPaused()


def skip_windows() -> list[tuple[datetime.datetime, datetime.datetime]]:
    """[from, to) ranges of jobs.created_at the AI sweeps leave out: each
    ended pause, from when it began to `ai_resume_backlog_days` before it
    ended. A pause still on has no window; nothing is read while it lasts."""
    keep = datetime.timedelta(days=int(db.get_config("ai_resume_backlog_days")))
    changes = db.query(
        "SELECT created_at, new_value FROM app_config_changes "
        "WHERE key = 'ai_paused' ORDER BY created_at, id"
    )
    windows = []
    began: datetime.datetime | None = None
    for change in changes:
        on = change["new_value"] is True
        if on and began is None:
            began = change["created_at"]
        elif not on and began is not None:
            if change["created_at"] - keep > began:
                windows.append((began, change["created_at"] - keep))
            began = None
    return windows


def params() -> dict[str, Any]:
    windows = skip_windows()
    return {
        "ai_skip_from": [w[0] for w in windows],
        "ai_skip_to": [w[1] for w in windows],
    }


# True when a posting falls in a pause's skipped backlog. Binds params().
SKIPPED = """EXISTS (
    SELECT 1 FROM unnest(%(ai_skip_from)s::timestamptz[], %(ai_skip_to)s::timestamptz[])
        AS ai_skip(from_at, to_at)
    WHERE {job}.created_at >= ai_skip.from_at AND {job}.created_at < ai_skip.to_at)"""
