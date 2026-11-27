"""Compute a person's For you ranking (api.for_you).

`rank_for_you` replaces one person's stored ranking with one computed from
their latest taste model, the scope and their board's conditions as they now
stand. It follows every fit of their model (tasks.taste), and runs every
cycle for everyone who has a model, so postings scoped or judged since are
taken in.
"""

from __future__ import annotations

import asyncio
from typing import Any

from api import db, for_you
from tasks.runtime import set_progress


async def handle_rank_for_you(task_id: int, payload: dict[str, Any]) -> None:
    user_id = int(payload["user_id"])
    window = int(db.get_config("recommendation_window_days"))
    stored = await asyncio.to_thread(for_you.compute, user_id, task_id, window)
    set_progress(
        task_id,
        stored["rows"],
        stored["postings"],
        f"ranked {stored['postings']} postings for user {user_id} into {stored['rows']} rows",
        extra=stored,
    )
