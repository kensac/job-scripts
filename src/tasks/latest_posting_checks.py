"""Fold dirty urls into latest_posting_checks (core.latest_posting_checks).

Every scheduler cycle a fold takes in the urls whose answers or postings
changed: production wrote about 1,213 non-custom answers (some 600 distinct
urls) and 888 new postings an hour over the week to 2026-11-26, the busiest
hour 6,743 answer urls. Until a url is folded a reader reads it from
`verdicts`, one probe each (about 0.03 ms on a production copy), so an
hourly fold keeps that to a fraction of a second; folding the busiest
hour's 6,743 urls took 0.7 s on the copy.

Once a day a fold over every job fills the projection the first time and
afterwards finds a write the triggers could not see. On the copy the first
fill wrote 1,574,826 rows in 27.5 s and a run with nothing to fix took
13.1 s, all of it recomputing every latest answer; hourly, that would be
5 minutes a day re-proving what the dirty log already says.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import latest_posting_checks
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)


async def handle_fold_latest_posting_checks(task_id: int, payload: dict[str, Any]) -> None:
    everything = bool(payload.get("everything"))
    counts = await asyncio.to_thread(latest_posting_checks.fold, everything=everything)
    logger.info("latest_posting_checks fold (everything=%s): %s", everything, counts)
    set_progress(
        task_id,
        counts["cleared"],
        counts["cleared"],
        f"wrote {counts['written']}, removed {counts['removed']}, "
        f"cleared {counts['cleared']} dirty rows",
        extra={**counts, "everything": everything},
    )
