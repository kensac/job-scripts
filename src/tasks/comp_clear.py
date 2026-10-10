"""Empties the pay columns on jobs that job_comp replaced.

Nothing reads or writes them since readers moved to job_comp, and every
answer they held was copied there first (copy_job_comp, checked on production
before the readers moved). Emptying them is what lets the migration that drops
them prove there is nothing left to lose. Idempotent by predicate: a run that
finds nothing only reads, and the worker offers it until a finished run
cleared nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import catalog
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# Rows per statement: 110,243 held pay on 2026-10-10, so about 23 statements.
BATCH = 5_000


async def handle_clear_jobs_pay(task_id: int, payload: dict[str, Any]) -> None:
    cleared = 0
    while not cancelled(task_id):
        n = await asyncio.to_thread(catalog.clear_pay, BATCH)
        if not n:
            break
        cleared += n
        set_progress(task_id, cleared, cleared, f"cleared pay on {cleared} jobs rows")
    set_progress(
        task_id, cleared, cleared, f"cleared pay on {cleared} jobs rows", {"cleared": cleared}
    )
    logger.info("clear_jobs_pay: cleared %s", cleared)
