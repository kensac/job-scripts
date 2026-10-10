"""Backfill for postings that were never scraped.

A job with no cached page is invisible to every check, so this is what makes
the rest of the sweeps reach it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from api.ai import verdicts
from core import catalog, verdict_reads
from core.store import MIN_CONTENT_CHARS, SUBSCRIBED_SOURCE
from tasks.board import fetch_retry_interval
from tasks.runtime import SCRAPE_CONCURRENCY, AdaptiveLimiter, cancelled, set_progress

logger = logging.getLogger(__name__)


async def handle_fetch_missing_content(task_id: int, payload: dict[str, Any]) -> None:
    """Jobs nobody ever scraped are invisible to every AI check. They can't be
    verified, filtered, or comp-extracted. This walks that backlog newest-first
    and caches their pages; the existing sweeps then pick them up for free.
    Self-limiting: once every job has content it finds nothing and costs
    nothing."""

    cap = max(1, payload.get("limit") or db.get_config("content_backfill_per_cycle"))
    rows = db.query(
        f"""
        SELECT j.url, j.company, j.title FROM jobs j
        WHERE {catalog.IS_AVAILABLE.format(job="j")} AND {SUBSCRIBED_SOURCE.format(source="j.source")}
          AND NOT EXISTS (
            SELECT 1 FROM page_texts q WHERE q.url = j.url
              AND length(q.input_content) > {MIN_CONTENT_CHARS})
          -- Any attempt waits out the base window, including one that
          -- stored too little text to count above; without this the backlog
          -- was the same dead postings every cycle.
          AND NOT EXISTS (
            SELECT 1 FROM page_fetches q WHERE q.url = j.url
              AND q.created_at > now() - %s::interval)
          -- A run of empty fetches waits longer each time, then stops.
          AND NOT {verdicts.fetch_parked_sql("j.url")}
          -- A posting its board reports gone has no page to fetch. That
          -- result is recorded as a closed verdict, not a content row, so
          -- the window above never saw it: 40 gone postings were re-fetched
          -- and re-verdicted every hour, 743 rows in a day (2026-09-06).
          -- The latest closed answer decides, as on the board: a posting
          -- rejected once and open since still has a page.
          AND {verdict_reads.latest_status("j.url", "closed")} IS DISTINCT FROM 'rejected'
        ORDER BY j.date_posted DESC NULLS LAST
        LIMIT %s
        """,
        (fetch_retry_interval(), cap),
    )
    if not rows:
        set_progress(task_id, 0, 0, "no content gaps")
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
                set_progress(task_id, done, total, f"fetched {fetched} pages")
        if cancelled(task_id):
            for tk in pending:
                tk.cancel()
            return
    set_progress(task_id, total, total, f"cached {fetched} of {total} pages")
