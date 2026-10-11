"""Catalog job writes: ingest upserts, corrections and uploads."""

from __future__ import annotations

import datetime
import logging
import random
import time
from typing import TYPE_CHECKING, Literal

from psycopg import errors, sql

from core.catalog.observations import (
    _BATCH,
    _LOCK_ORDER,
    IS_AVAILABLE,
    set_active,
)
from core.pool import pool, statement, transaction

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
    posting_rows = [
        (
            p.url,
            p.raw_url or p.url,
            p.company,
            p.title,
            p.locations,
            p.terms,
            source,
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
    posting_rows.sort(key=lambda row: row[0])
    for start in range(0, len(posting_rows), _BATCH):
        _upsert_batch(posting_rows[start : start + _BATCH])
    return len(posting_rows)


def set_near_copy_keys(keys: dict[str, str]) -> None:
    """Records the near-copy key (core.near_copy) of the text verification is
    about to read, by url. Several rows in one statement, so they are locked
    in url order (_LOCK_ORDER) like every multi-row writer of jobs: ingest
    upserts the same urls concurrently."""
    if not keys:
        return
    with pool.connection() as conn:
        conn.execute(
            f"""
            WITH locked AS (
                SELECT j.id, k.key
                FROM jobs j JOIN unnest(%s::text[], %s::text[]) AS k(url, key) ON k.url = j.url
                WHERE j.near_copy_key IS DISTINCT FROM k.key
                ORDER BY j.url {_LOCK_ORDER} FOR UPDATE OF j
            )
            UPDATE jobs SET near_copy_key = locked.key FROM locked WHERE jobs.id = locked.id
            """,
            (list(keys), list(keys.values())),
        )


# What an administrator may correct by hand (PATCH /admin/jobs/{id}).
_CORRECTABLE = ("company", "title", "locations", "terms")


def correct_posting(job_id: int, fields: dict) -> dict | None:
    """Applies an administrator's correction and returns the row as it is
    left, or None when no such job. `active` goes through set_active."""
    unknown = set(fields) - {*_CORRECTABLE, "active"}
    if unknown:
        raise ValueError(f"not correctable: {sorted(unknown)}")
    with transaction():
        if "active" in fields and not set_active(job_id, fields["active"]):
            return None
        columns = [c for c in _CORRECTABLE if c in fields]
        with statement() as conn:
            if columns:
                conn.execute(
                    sql.SQL("UPDATE jobs SET {} WHERE id = %(jid)s").format(
                        sql.SQL(", ").join(
                            sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder(c))
                            for c in columns
                        )
                    ),
                    {"jid": job_id, **{c: fields[c] for c in columns}},
                )
            return conn.execute(
                "SELECT j.id, j.url, j.company, j.title, j.locations, j.terms, "
                f"{IS_AVAILABLE.format(job='j')} AS active FROM jobs j WHERE j.id = %s",
                (job_id,),
            ).fetchone()


def fill_date_posted(url: str, posted: datetime.date) -> None:
    """Only where the board's listing left it empty: a date the listing stated
    is the same fact from the same board, and never worse."""
    with statement() as conn:
        conn.execute(
            "UPDATE jobs SET date_posted = %s WHERE url = %s AND date_posted IS NULL",
            (posted, url),
        )


# An upload's state lives in posting_uploads alone. jobs.extraction_status
# is frozen: nothing writes it, and it keeps the done stamp the 2026-08-24
# sheet import put on its rows and one forced reparse left (6,022 rows), which
# is recorded nowhere else. A writer that touches both tables takes the jobs
# row first, so the two locks are never taken in opposite orders.


def add_upload(url: str, raw_url: str, user_id: int) -> dict:
    """A person's own posting. A url the catalog already holds keeps its row,
    and saving it writes no upload: it only tracks it. An upload whose
    extraction failed goes back to pending. Returns id, extraction_status and
    uploaded_by (both None for a catalog posting), which the caller checks for
    another person's upload. Joins the caller's transaction."""
    with transaction(), statement() as conn:
        # DO NOTHING waits for a concurrent insert of the same url to commit,
        # so the SELECT below and the upload read see its row.
        inserted = conn.execute(
            # Available from the start (_STORED): no source lists a person's
            # own posting, and readers read jobs.available alone.
            "INSERT INTO jobs (url, raw_url, source, available) VALUES (%s, %s, 'upload', true) "
            "ON CONFLICT (url) DO NOTHING RETURNING id",
            (url, raw_url),
        ).fetchone()
        if inserted is not None:
            conn.execute(
                "INSERT INTO posting_uploads (job_id, uploaded_by, status) "
                "VALUES (%s, %s, 'pending')",
                (inserted["id"], user_id),
            )
            return {"id": inserted["id"], "extraction_status": "pending", "uploaded_by": user_id}
        job = conn.execute("SELECT id FROM jobs WHERE url = %s", (url,)).fetchone()
        assert job is not None
        upload = conn.execute(
            "UPDATE posting_uploads SET status = "
            "CASE WHEN status = 'failed' THEN 'pending' ELSE status END "
            "WHERE job_id = %s RETURNING uploaded_by, status",
            (job["id"],),
        ).fetchone()
    return {
        "id": job["id"],
        "extraction_status": upload["status"] if upload else None,
        "uploaded_by": upload["uploaded_by"] if upload else None,
    }


def set_extraction_status(job_id: int, status: Literal["pending", "failed"]) -> None:
    """A forced reparse of a posting nobody uploaded has no row to mark; its
    outcome is its task's."""
    with statement() as conn:
        conn.execute("UPDATE posting_uploads SET status = %s WHERE job_id = %s", (status, job_id))


def record_extraction(
    job_id: int, company: str, title: str, locations: list[str], terms: list[str]
) -> None:
    """What the extractor read off the page of an upload or a forced reparse."""
    with transaction(), statement() as conn:
        conn.execute(
            "UPDATE jobs SET company = %s, title = %s, locations = %s, terms = %s WHERE id = %s",
            (company, title, locations, terms, job_id),
        )
        conn.execute("UPDATE posting_uploads SET status = 'done' WHERE job_id = %s", (job_id,))


def _upsert_batch(batch: list[tuple], retries: int = 3) -> None:
    for attempt in range(retries):
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                # Uploads this pull will take over (source becomes the feed's,
                # the SET below): read before the upsert, because afterwards
                # they no longer say upload. Driven by upload rows not yet
                # done, which is none almost always.
                taken = [
                    r["job_id"]
                    for r in cur.execute(
                        "SELECT p.job_id FROM posting_uploads p JOIN jobs j ON j.id = p.job_id "
                        "WHERE p.status <> 'done' AND j.source = 'upload' AND j.url = ANY(%s)",
                        ([row[0] for row in batch],),
                    ).fetchall()
                ]
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
                INSERT INTO jobs (url, raw_url, company, title, locations, terms, source,
                                  available, date_posted)
                SELECT v.url, v.raw_url, v.company, v.title, v.locations, v.terms, v.source,
                       true, v.date_posted
                FROM (VALUES (%s::text, %s::text, %s::text, %s::text, %s::text[], %s::text[],
                              %s::text, %s::timestamptz))
                    AS v (url, raw_url, company, title, locations, terms, source, date_posted)
                WHERE NOT EXISTS (
                    SELECT 1 FROM jobs j
                    WHERE j.url = v.url
                      AND (CASE WHEN j.source = 'upload' OR j.company = ''
                                THEN v.company ELSE j.company END,
                           CASE WHEN j.source = 'upload' OR j.title = ''
                                THEN v.title ELSE j.title END,
                           v.locations, v.terms,
                           COALESCE(j.date_posted, v.date_posted),
                           CASE WHEN j.source = 'upload' THEN v.source ELSE j.source END)
                          IS NOT DISTINCT FROM
                          (j.company, j.title, j.locations, j.terms,
                           j.date_posted, j.source)
                )
                ON CONFLICT (url) DO UPDATE SET
                    company = CASE WHEN jobs.source = 'upload' OR jobs.company = ''
                                   THEN EXCLUDED.company ELSE jobs.company END,
                    title = CASE WHEN jobs.source = 'upload' OR jobs.title = ''
                                 THEN EXCLUDED.title ELSE jobs.title END,
                    locations = EXCLUDED.locations,
                    terms = EXCLUDED.terms,
                    date_posted = COALESCE(jobs.date_posted, EXCLUDED.date_posted),
                    source = CASE WHEN jobs.source = 'upload'
                                  THEN EXCLUDED.source ELSE jobs.source END
                        """,
                    batch,
                )
                # A pull that lists an upload's url takes the row over, and
                # the feed's text replaces the extraction: the upload is done.
                # The jobs rows it changed are locked by this transaction.
                if taken:
                    cur.execute(
                        "UPDATE posting_uploads SET status = 'done' "
                        "WHERE job_id = ANY(%s) AND status <> 'done'",
                        (taken,),
                    )
            return
        except errors.DeadlockDetected:
            if attempt == retries - 1:
                raise
            delay = random.uniform(0.2, 1.0) * (attempt + 1)  # noqa: S311 - retry jitter
            logger.warning(f"Catalog upsert deadlock, retrying in {delay:.1f}s")
            time.sleep(delay)
