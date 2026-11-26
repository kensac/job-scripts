"""Keep latest_posting_checks matching `verdicts` (core.latest_posting_checks).

Daily. The triggers keep the projection exact as answers and postings are
written; this run fills it the first time and afterwards finds a write the
triggers could not see. On a production copy (2026-11-26) the first fill
wrote 1,574,826 rows in 29 s and a run with nothing to fix took 23 s, all of
it recomputing every posting's latest answers. Once a day bounds a missed
write to a day for 23 s of database time; hourly would spend 9 minutes a day
re-proving what the triggers already keep.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import latest_posting_checks
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)


async def handle_reconcile_latest_posting_checks(task_id: int, payload: dict[str, Any]) -> None:
    counts = await asyncio.to_thread(latest_posting_checks.reconcile)
    written = sum(counts.values())
    # After the first fill, any row written here is a write the triggers
    # missed; the counts stay on the task to be found.
    logger.info("latest_posting_checks reconcile: %s", counts)
    set_progress(
        task_id,
        written,
        written,
        f"filled {counts['filled']}, replaced {counts['replaced']}, removed {counts['removed']}",
        extra=counts,
    )
