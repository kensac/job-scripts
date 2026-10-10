"""Empties listings.pattern, pointing each row at the stored copy of its text.

A pull empties each row it rewrites, so this reaches the rest: rows no board
lists any more, kept until screened_retention_days, and rows no pull has
rewritten since the copy stopped being written. It is idempotent by predicate
(pattern IS NOT NULL): a second run finds nothing, and a run cut short
resumes where the rows are. Once a run starts with none left, the column is
empty and can be dropped (migrations.md).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import catalog
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

LABEL = "emptying the title pattern copies on listings"


async def handle_drop_listing_pattern_copies(task_id: int, payload: dict[str, Any]) -> None:
    left = await asyncio.to_thread(catalog.listings_holding_a_pattern_copy)
    total, done = sum(left.values()), 0
    set_progress(task_id, 0, total, LABEL)
    for source in sorted(left):
        while not cancelled(task_id):
            n = await asyncio.to_thread(catalog.drop_pattern_copies, source)
            if not n:
                break
            done += n
            set_progress(task_id, done, total, LABEL)
    set_progress(task_id, done, total, f"emptied {done} of {total} listings", {"emptied": done})
    logger.info("drop_listing_pattern_copies: %s of %s", done, total)
