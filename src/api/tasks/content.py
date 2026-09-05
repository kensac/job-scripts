"""Backfill for postings that were never scraped.

A job with no cached page is invisible to every check, so this is what makes
the rest of the sweeps reach it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from api import db, verdicts
from api.tasks.board import fetch_retry_interval
from api.tasks.runtime import SCRAPE_CONCURRENCY, AdaptiveLimiter, _cancelled, _set_progress
from core.store import SUBSCRIBED_SOURCE

logger = logging.getLogger("jobtracker_worker")


CONTENT_BACKFILL_PER_CYCLE = int(os.environ.get("JOBTRACKER_CONTENT_BACKFILL_PER_CYCLE", "100"))


async def handle_fetch_missing_content(task_id: int, payload: dict[str, Any]) -> None:
    """Jobs nobody ever scraped are invisible to every AI check. They can't be
    verified, filtered, or comp-extracted. This walks that backlog newest-first
    and caches their pages; the existing sweeps then pick them up for free.
    Self-limiting: once every job has content it finds nothing and costs
    nothing."""

    cap = max(1, payload.get("limit") or CONTENT_BACKFILL_PER_CYCLE)
    rows = db.query(
        f"""
        SELECT j.url, j.company, j.title FROM jobs j
        WHERE j.active AND {SUBSCRIBED_SOURCE.format(source="j.source")}
          AND NOT EXISTS (
            SELECT 1 FROM ai_queries q WHERE q.url = j.url
              AND q.input_content IS NOT NULL AND length(q.input_content) > 200)
          -- A fetch that came back empty is not retried inside the window;
          -- without this the backlog was the same dead postings every cycle.
          AND NOT EXISTS (
            SELECT 1 FROM ai_queries q WHERE q.url = j.url AND q.check_type = 'content'
              AND q.created_at > now() - %s::interval)
        ORDER BY j.date_posted DESC NULLS LAST
        LIMIT %s
        """,
        (fetch_retry_interval(), cap),
    )
    if not rows:
        _set_progress(task_id, 0, 0, "no content gaps")
        return
    total = len(rows)
    done = fetched = 0
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def one(r: dict[str, Any]) -> bool:
        content, _closure = await verdicts.refresh_content(
            r["url"],
            company=r["company"],
            job_title=r["title"],
            context="content-backfill",
            scrape_sem=scrape_sem,
        )
        return bool(content)

    limiter = AdaptiveLimiter()
    idx = 0
    pending: dict[asyncio.Task, dict[str, Any]] = {}
    while idx < total or pending:
        while idx < total and len(pending) < limiter.limit:
            pending[asyncio.create_task(one(rows[idx]))] = rows[idx]
            idx += 1
        if not pending:
            break
        finished, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for tk in finished:
            r = pending.pop(tk)
            done += 1
            try:
                if tk.result():
                    fetched += 1
                limiter.record()
            except Exception:
                limiter.record(error=True)
                logger.warning(f"content backfill failed for {r['url']}")
            if done % 10 == 0:
                _set_progress(task_id, done, total, f"fetched {fetched} pages")
        if _cancelled(task_id):
            for tk in pending:
                tk.cancel()
            return
    _set_progress(task_id, total, total, f"cached {fetched} of {total} pages")
