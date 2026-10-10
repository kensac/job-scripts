"""Copies the uploads on jobs into posting_uploads (core.catalog).

Idempotent by predicate: a row is written only where posting_uploads is
missing it or holds another status, so a second run writes nothing and a run
cut short resumes where the rows are. Offered every cycle while jobs still
carries the upload columns, because a server on the older release writes
only those.
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


async def handle_backfill_posting_uploads(task_id: int, payload: dict[str, Any]) -> None:
    written = seen = no_status = after = 0
    while not cancelled(task_id):
        row = await asyncio.to_thread(catalog.reconcile_uploads, after, BATCH)
        if row["last"] is None:
            break
        after = row["last"]
        written += row["written"]
        no_status += row["no_status"]
        seen += 1
        set_progress(task_id, written, written, "copying uploads into posting_uploads")
    # An upload with no extraction status has no row to give it; counted so
    # a nonzero number is seen rather than skipped silently (0 on 2026-10-10).
    extra = {"written": written, "chunks": seen, "no_status": no_status}
    set_progress(task_id, written, written, f"wrote {written} upload rows", extra)
    logger.info("backfill_posting_uploads: %s", extra)
