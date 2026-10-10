"""Empties the copies uploads left in jobs.uploaded_by and
jobs.extraction_status (core.catalog.clear_upload_columns).

posting_uploads holds upload state and nothing reads or writes the two
columns. A migration drops uploaded_by once it proves it empty;
extraction_status keeps the values that are not copies and is frozen.
Idempotent by predicate: a run touches only rows that still hold a copy, so a
second run clears nothing and a run cut short resumes. Offered every cycle
until the drop, because a server on the release before this one still writes
them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import catalog
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# Uploads per statement. 11 on 2026-10-10, so one statement; the bound keeps
# a run's row locks short if uploads grow.
BATCH = 500


async def handle_clear_upload_columns(task_id: int, payload: dict[str, Any]) -> None:
    cleared = unmatched = after = 0
    while not cancelled(task_id):
        row = await asyncio.to_thread(catalog.clear_upload_columns, after, BATCH)
        if row["last"] is None:
            break
        after = row["last"]
        cleared += row["cleared"]
        unmatched += row["unmatched"]
        set_progress(task_id, cleared, cleared, "clearing jobs upload columns")
    # An uploader posting_uploads does not name is kept and counted, so a
    # nonzero number is seen; the drop refuses while it is there.
    extra = {"cleared": cleared, "unmatched": unmatched}
    set_progress(task_id, cleared, cleared, f"cleared {cleared} rows", extra)
    logger.info("clear_upload_columns: %s", extra)
