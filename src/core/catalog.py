from __future__ import annotations

import datetime
import logging
import random
import time
from typing import TYPE_CHECKING

from psycopg import errors
from psycopg.types.json import Jsonb

from core.pool import pool

if TYPE_CHECKING:
    from core.fetching.posting import JobPosting

logger = logging.getLogger(__name__)

_TABLE_READY = False


def _ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    with pool.connection() as conn:
        exists = conn.execute("SELECT to_regclass('jobs') AS t").fetchone()
        _TABLE_READY = bool(exists and exists["t"])
    if not _TABLE_READY:
        logger.info("jobs catalog table missing; skipping catalog upserts")


def upsert_postings(postings: list[JobPosting], source: str) -> int:
    _ensure_table()
    if not _TABLE_READY or not postings:
        return 0
    rows = [
        (
            p.url,
            p.raw_url or p.url,
            p.company,
            p.title,
            p.locations,
            p.terms,
            source,
            p.active,
            datetime.datetime.fromtimestamp(p.date_posted, tz=datetime.UTC)
            if p.date_posted
            else None,
        )
        for p in postings
        if p.url
    ]
    # Concurrent ingest tasks upsert overlapping url sets (boards share jobs).
    # Deterministic ordering + small per-transaction batches + a deadlock retry
    # keep fleet workers from deadlocking each other on the jobs unique index.
    rows.sort(key=lambda r: r[0])
    for start in range(0, len(rows), _BATCH):
        _upsert_batch(rows[start : start + _BATCH])
    return len(rows)


def retire_unlisted(source: str, listed_and_admitted: list[str]) -> int:
    """Marks inactive every active row of this source that the pull did not
    admit. Only for a board that lists every open posting (boards.AUTHORITATIVE),
    where absence is closure; the caller decides that. Rows come back active
    through upsert_postings when the board lists them and the pattern admits
    them again, so a pattern change in either direction is one pull away."""
    with pool.connection() as conn:
        dropped = conn.execute(
            "UPDATE jobs SET active = false WHERE source = %s AND active AND url <> ALL(%s) "
            "RETURNING id",
            (source, listed_and_admitted),
        ).fetchall()
        # The board stopped listing it, which is an observation. jobs.active
        # is the current answer; this is how it got there, and a boolean
        # cannot say how many times it has changed.
        if dropped:
            conn.cursor().executemany(
                "INSERT INTO job_listing_events (job_id, source, listed) VALUES (%s, %s, false)",
                [(row["id"], source) for row in dropped],
            )
        return len(dropped)


_BATCH = 500


def _upsert_batch(batch: list[tuple], retries: int = 3) -> None:
    # The urls this pull says are open. Which of them the catalog currently
    # holds as inactive is read BEFORE the upsert, because afterwards they all
    # look the same: the transition is only visible from the old row.
    proposed = [row[0] for row in batch if row[7]]
    for attempt in range(retries):
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                returning = (
                    cur.execute(
                        "SELECT id, url, source FROM jobs WHERE url = ANY(%s) AND NOT active",
                        (proposed,),
                    ).fetchall()
                    if proposed
                    else []
                )
                cur.executemany(
                    """
                INSERT INTO jobs (url, raw_url, company, title, locations, terms, source, active, date_posted)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (url) DO UPDATE SET
                    company = CASE WHEN jobs.source = 'upload' OR jobs.company = ''
                                   THEN EXCLUDED.company ELSE jobs.company END,
                    title = CASE WHEN jobs.source = 'upload' OR jobs.title = ''
                                 THEN EXCLUDED.title ELSE jobs.title END,
                    locations = EXCLUDED.locations,
                    terms = EXCLUDED.terms,
                    active = EXCLUDED.active,
                    date_posted = COALESCE(jobs.date_posted, EXCLUDED.date_posted),
                    source = CASE WHEN jobs.source = 'upload'
                                  THEN EXCLUDED.source ELSE jobs.source END,
                    extraction_status = CASE WHEN jobs.source = 'upload'
                                             THEN 'done' ELSE jobs.extraction_status END
                        """,
                    batch,
                )
                # The feed put these back after having dropped them. One row
                # per return, which is what makes a flapping board countable.
                if returning:
                    cur.executemany(
                        "INSERT INTO job_listing_events (job_id, source, listed) "
                        "VALUES (%s, %s, true)",
                        [(r["id"], r["source"]) for r in returning],
                    )
            return
        except errors.DeadlockDetected:
            if attempt == retries - 1:
                raise
            delay = random.uniform(0.2, 1.0) * (attempt + 1)  # noqa: S311 - retry jitter
            logger.warning(f"Catalog upsert deadlock, retrying in {delay:.1f}s")
            time.sleep(delay)


def record_listings(
    postings: list[JobPosting], source: str, pattern: str, kept: set[str], retention_days: int
) -> int:
    """Keeps everything a board listed, refreshed on every pull: the title
    (so a candidate pattern is judged against a month of real titles), the
    posting text when the listing call carried it (so a backfill never
    scrapes a page the board already handed over), and the raw record minus
    that text (so a backtest can read a field nobody mapped). `kept` names
    the urls the title pattern admitted. Rows the board has stopped listing
    age out after retention_days. Nothing downstream reads this table."""
    rows = [
        (
            p.url,
            source,
            p.company,
            p.title,
            p.locations,
            datetime.datetime.fromtimestamp(p.date_posted, tz=datetime.UTC)
            if p.date_posted
            else None,
            pattern,
            p.url in kept,
            p.description or "",
            Jsonb(p.raw or {}),
        )
        for p in postings
        if p.url
    ]
    with pool.connection() as conn, conn.cursor() as cur:
        if rows:
            cur.executemany(
                """
                INSERT INTO listings
                    (url, source, company, title, locations, date_posted, pattern,
                     kept, description, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (url) DO UPDATE SET
                    source = EXCLUDED.source, company = EXCLUDED.company,
                    title = EXCLUDED.title, locations = EXCLUDED.locations,
                    date_posted = COALESCE(listings.date_posted, EXCLUDED.date_posted),
                    pattern = EXCLUDED.pattern, kept = EXCLUDED.kept,
                    description = CASE WHEN EXCLUDED.description = ''
                                       THEN listings.description ELSE EXCLUDED.description END,
                    raw = EXCLUDED.raw, last_seen_at = now()
                """,
                rows,
            )
        cur.execute(
            "DELETE FROM listings WHERE source = %s "
            "AND last_seen_at < now() - make_interval(days => %s)",
            (source, retention_days),
        )
    return len(rows)
