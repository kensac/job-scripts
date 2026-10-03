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
                # A row is written only when the update would change it.
                # Rewriting every listed row was 1.33M updates in 36 hours on
                # a 605k-row catalog, 5.7 GB of WAL, while the feeds put back
                # 5,336 postings (pg_stat_statements and job_listing_events,
                # 2026-10-03). The NOT EXISTS is the SET below evaluated
                # against the stored row, so it must change whenever the SET
                # does. It filters in the SELECT rather than in a WHERE on DO
                # UPDATE because DO UPDATE locks, and logs, the conflicting
                # row even when its WHERE refuses the update.
                cur.executemany(
                    """
                INSERT INTO jobs (url, raw_url, company, title, locations, terms, source, active, date_posted)
                SELECT v.url, v.raw_url, v.company, v.title, v.locations, v.terms, v.source,
                       v.active, v.date_posted
                FROM (VALUES (%s::text, %s::text, %s::text, %s::text, %s::text[], %s::text[],
                              %s::text, %s::boolean, %s::timestamptz))
                    AS v (url, raw_url, company, title, locations, terms, source, active,
                          date_posted)
                WHERE NOT EXISTS (
                    SELECT 1 FROM jobs j
                    WHERE j.url = v.url
                      AND (CASE WHEN j.source = 'upload' OR j.company = ''
                                THEN v.company ELSE j.company END,
                           CASE WHEN j.source = 'upload' OR j.title = ''
                                THEN v.title ELSE j.title END,
                           v.locations, v.terms, v.active,
                           COALESCE(j.date_posted, v.date_posted),
                           CASE WHEN j.source = 'upload' THEN v.source ELSE j.source END,
                           CASE WHEN j.source = 'upload'
                                THEN 'done' ELSE j.extraction_status END)
                          IS NOT DISTINCT FROM
                          (j.company, j.title, j.locations, j.terms, j.active,
                           j.date_posted, j.source, j.extraction_status)
                )
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
    postings: list[JobPosting],
    source: str,
    pattern: str,
    kept: set[str],
    retention_days: int,
    refresh_hours: int,
) -> int:
    """Keeps everything a board listed: the title (so a candidate pattern is
    judged against a month of real titles), the posting text when the
    listing call carried it (so a backfill never scrapes a page the board
    already handed over), and the raw record minus that text (so a backtest
    can read a field nobody mapped). `kept` names the URLs the stored title
    pattern matched, including while pattern enforcement is disabled and
    every posting enters the catalog. Read by the admin source screens
    (pattern preview, screened postings), never by visibility or the checks.

    A row is rewritten only when what it would hold differs, or when its
    last_seen_at is older than refresh_hours. Rewriting every listed row on
    every pull was 9.17 GB of WAL a day, 19% of the cluster's, for rows
    99.2% unchanged (pg_stat_statements over 24.6 hours, 2026-10-03). The
    unchanged rows are filtered out before the insert rather than by a
    WHERE on DO UPDATE, because DO UPDATE locks the conflicting row even
    when its WHERE refuses the update, and the lock is itself a logged page
    write: 1,174 bytes of WAL a row against 0 for the filter, measured on
    3,000 rows after a checkpoint.

    Rows the board stopped listing age out retention_days after the pull
    that last listed them, counted from last_seen_at plus refresh_hours
    because last_seen_at can lag that pull by up to refresh_hours. So a row
    is never deleted sooner than before and at most refresh_hours later. A
    row still listed is never deleted: the pull refreshes it first."""
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
            refresh_hours,
        )
        for p in postings
        if p.url
    ]
    with pool.connection() as conn, conn.cursor() as cur:
        if rows:
            # The NOT EXISTS is the update's own SET, evaluated against the
            # row: what the row would hold after it equals what it holds now.
            # date_posted keeps the first date seen, so a board that dates by
            # age ("Posted 3 Days Ago", Workday and the markdown lists), which
            # yields a new timestamp on every pull, is not a change. An empty
            # description keeps the stored one. description and raw are set
            # from the old row when equal, because Postgres reuses a TOASTed
            # value only when handed the old row's own pointer; a value from
            # EXCLUDED is a fresh copy, written out again chunk by chunk.
            cur.executemany(
                """
                INSERT INTO listings
                    (url, source, company, title, locations, date_posted, pattern,
                     kept, description, raw)
                SELECT v.url, v.source, v.company, v.title, v.locations, v.date_posted,
                       v.pattern, v.kept, v.description, v.raw
                FROM (VALUES (%s::text, %s::text, %s::text, %s::text, %s::text[],
                              %s::timestamptz, %s::text, %s::boolean, %s::text, %s::jsonb,
                              %s::integer))
                    AS v (url, source, company, title, locations, date_posted, pattern,
                          kept, description, raw, refresh_hours)
                WHERE NOT EXISTS (
                    SELECT 1 FROM listings l
                    WHERE l.url = v.url
                      AND (l.source, l.company, l.title, l.locations, l.pattern, l.kept)
                          IS NOT DISTINCT FROM
                          (v.source, v.company, v.title, v.locations, v.pattern, v.kept)
                      AND (l.date_posted IS NOT NULL OR v.date_posted IS NULL)
                      AND v.description IN ('', l.description)
                      AND v.raw = l.raw
                      AND l.last_seen_at >= now() - make_interval(hours => v.refresh_hours)
                )
                ON CONFLICT (url) DO UPDATE SET
                    source = EXCLUDED.source, company = EXCLUDED.company,
                    title = EXCLUDED.title, locations = EXCLUDED.locations,
                    date_posted = COALESCE(listings.date_posted, EXCLUDED.date_posted),
                    pattern = EXCLUDED.pattern, kept = EXCLUDED.kept,
                    description = CASE WHEN EXCLUDED.description IN ('', listings.description)
                                       THEN listings.description ELSE EXCLUDED.description END,
                    raw = CASE WHEN EXCLUDED.raw = listings.raw
                               THEN listings.raw ELSE EXCLUDED.raw END,
                    last_seen_at = now()
                """,
                rows,
            )
        cur.execute(
            "DELETE FROM listings WHERE source = %s "
            "AND last_seen_at < now() - make_interval(days => %s, hours => %s)",
            (source, retention_days, refresh_hours),
        )
    return len(rows)
