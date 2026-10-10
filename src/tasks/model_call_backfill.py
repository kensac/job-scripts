"""Copies the paid calls made before model_calls existed into it.

The steps are api.model_calls's, in the order they depend on each other:
verdict items before the receipts they leave over, both before a batch's
remainder. Each is idempotent by predicate, so a run cut short resumes and a
finished run is not queued again (api.worker).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import model_calls
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

_STEPS = 5


async def handle_backfill_model_calls(task_id: int, payload: dict[str, Any]) -> None:
    cutover = await asyncio.to_thread(model_calls.backfill_cutover)
    written: dict[str, int] = {}

    def done(step: str, n: int) -> None:
        written[step] = n
        set_progress(task_id, len(written), _STEPS, f"{step}: {n} calls", {"written": written})

    done("batched_verdicts", await asyncio.to_thread(model_calls.backfill_verdicts, False, cutover))
    receipts = 0
    while not cancelled(task_id):
        n = await asyncio.to_thread(model_calls.backfill_receipts)
        if not n:
            break
        receipts += n
        set_progress(task_id, 1, _STEPS, f"receipts: {receipts} calls so far")
    if cancelled(task_id):
        return
    done("receipts", receipts)
    done("batch_remainders", await asyncio.to_thread(model_calls.backfill_remainders))
    done("live_verdicts", await asyncio.to_thread(model_calls.backfill_verdicts, True, cutover))
    done("usage", await asyncio.to_thread(model_calls.backfill_usage, cutover))
    logger.info("backfill_model_calls: %s (cutover %s)", written, cutover)
