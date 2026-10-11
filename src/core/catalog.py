from __future__ import annotations

import datetime
import logging
import random
import time
from collections import Counter
from typing import TYPE_CHECKING, Literal, LiteralString

from psycopg import errors, sql
from psycopg.types.json import Jsonb

from core import listing_payloads
from core.pool import connection, in_transaction, pool, statement, transaction

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


# Whether listings row {listing} is on its source's latest pull. Rows are
# never deleted, so existence says only that some pull once listed the url.
# A pull refreshes every row it lists whose last_seen_at is older than
# refresh_hours, so right after a pull every row it listed is at most
# refresh_hours older than that pull; the source's newest last_seen_at is no
# later than its latest pull, so a listed row is never older than that minus
# refresh_hours. A row the board dropped falls behind within refresh_hours
# plus one pull interval. A source whose pulls fail keeps its rows current,
# because nothing newer moves its newest row. Measured on production
# 2026-10-10: 1.6 s for retire_switched_off's select with it, 0.6 s without,
# reading each candidate source's rows once per candidate. {source} is the
# row's source column, or a parameter when the reader is one source's rows:
# correlated to the row, the max is re-read for every row, which over one
# source's 25k rows did not finish in 120 s; a parameter makes it one
# InitPlan.
# ponytail: max() per source scans that source's rows; a per-source
# latest-pull column is the upgrade if a source's listings outgrow it.
LISTED_NOW: LiteralString = (
    "{listing}.last_seen_at >= (SELECT max(newest.last_seen_at) FROM listings newest "
    "WHERE newest.source = {source}) - make_interval(hours => %(refresh_hours)s)"
)


# Every write to jobs is in this module; tests/test_catalog_one_writer.py fails
# on one anywhere else. The writes below are single-row, so they take one row
# lock and have no order to keep (_LOCK_ORDER is for multi-row writes).


def set_active(job_id: int, active: bool) -> bool:
    """An administrator's correction of whether a posting is available,
    recorded as an observation under CORRECTION_SOURCE and stored in
    jobs.available in the same transaction: it holds until a source says
    something new about the posting. jobs.active is frozen and not written
    (docs/agents/architecture-migration.md, the jobs.active contract). False
    when no such job."""
    with transaction(), connection() as conn:
        if conn.execute("SELECT 1 FROM jobs WHERE id = %s", (job_id,)).fetchone() is None:
            return False
        conn.execute(
            "INSERT INTO source_observations (job_id, source, kind) VALUES (%s, %s, %s)",
            (job_id, CORRECTION_SOURCE, "reappeared" if active else "unlisted"),
        )
        _refresh_available(conn, [job_id], skip_locked=False)
        return True


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


_ADMITTED = frozenset({"appeared", "reappeared"})


# The source an administrator's correction is recorded under (set_active).
# No sources row has this name, so no pull and no switch reaches it.
CORRECTION_SOURCE = "admin"

# Whether title patterns are enforced, read where availability is read so that
# flipping source_title_patterns_enabled never rewrites history. An unseeded
# row means the seed has not run yet, and reads as the seeded default
# (api.config), which tests/test_source_observations.py holds equal.
PATTERNS_ENFORCED: LiteralString = (
    "(COALESCE((SELECT c.value FROM app_config c"
    " WHERE c.key = 'source_title_patterns_enabled'), 'true') = 'true')"
)

# Whether job {job} is available, as source_observations say. In order:
# - an administrator's correction (CORRECTION_SOURCE) holds while it is the
#   job's latest observation, so it lasts until a source says something new;
# - true when some switched-on source's latest observation admits it:
#   appeared, reappeared, not_listed (aggregator absence never closes), or
#   filtered while patterns are not enforced;
# - false when it has observations and none of those;
# - false when no source has observed it and its owning source is switched
#   off: nothing that is pulled says it is listed;
# - false when it is a sheet_import row: imported once on 2026-08-24, with no
#   sources row and nothing that pulls it, so it is a switched-off source
#   (decided 2026-10-10). A person who touched one keeps it through their own
#   state, which visibility reads separately;
# - otherwise NULL, cannot tell.
# Parameter-free, so it drops into SQL of either placeholder style. One
# definition, for the shadow comparison and, through IS_AVAILABLE, readers.
AVAILABLE: LiteralString = (
    """(
    COALESCE(
        (SELECT CASE WHEN o.source = '"""
    + CORRECTION_SOURCE
    + """' THEN o.kind = 'reappeared' END
         FROM source_observations o WHERE o.job_id = {job}.id ORDER BY o.id DESC LIMIT 1),
        CASE WHEN EXISTS (
                SELECT 1 FROM (
                    SELECT DISTINCT ON (o.source) o.source, o.kind FROM source_observations o
                    WHERE o.job_id = {job}.id ORDER BY o.source, o.id DESC) latest
                JOIN sources s ON s.name = latest.source AND s.active
                WHERE latest.kind IN ('appeared', 'reappeared', 'not_listed')
                   OR (latest.kind = 'filtered' AND NOT """
    + PATTERNS_ENFORCED
    + """))
             THEN true
             WHEN EXISTS (SELECT 1 FROM source_observations o WHERE o.job_id = {job}.id)
             THEN false
             WHEN EXISTS (SELECT 1 FROM sources s WHERE s.name = {job}.source AND NOT s.active)
             THEN false
             WHEN {job}.source = 'sheet_import' THEN false
        END))"""
)

# What a reader of availability uses: jobs.available (_STORED) alone. A new
# row is stored as it is inserted (upsert_postings, add_upload) and every
# other row by the reconcile, so a NULL is a row nothing has stored yet and
# reads as not available. Stored, because computed per row it cost every
# reader: the AI-eligible count went from 2.0 s to 6.5 s, a board recompute
# from 27 s to 38 s (production, 2026-10-10).
IS_AVAILABLE: LiteralString = "COALESCE({job}.available, false)"

# What jobs.available stores: AVAILABLE where an observation decides, and
# otherwise the posting's last known feed state, jobs.active. A row no pull
# has observed since observations began keeps what its feed last said rather
# than reading as cannot tell: a partial pull (a capped Workday or Oracle
# search) never observes the rows past its cap and records no absence, and a
# board not pulled yet has observed nothing. On 2026-10-11 at 04:10 UTC that
# was 102,662 active postings, 81,644 of them on boards not yet pulled and
# 14,693 behind a partial pull. Re-verification closes what a partial pull
# cannot see (docs/agents/sources-and-boards.md).
_STORED: LiteralString = "COALESCE(" + AVAILABLE + ", {job}.active)"

_RECONCILE_CHUNK = 20_000


def _refresh_available(conn, ids: list[int], skip_locked: bool) -> int:
    """Writes _STORED into jobs.available for these rows where it differs.
    Locks first, in url order (_LOCK_ORDER), then evaluates in a second
    statement: under READ COMMITTED that statement's snapshot is taken after
    the locks are held, so every observation committed before them is seen,
    and a writer that commits one after them refreshes again behind this
    one. Evaluated with the lock in one statement, the value would come from
    a snapshot older than the lock. Returns the rows written."""
    if not ids:
        return 0
    locked = [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM jobs WHERE id = ANY(%s) ORDER BY url {_LOCK_ORDER} "
            f"FOR NO KEY UPDATE{' SKIP LOCKED' if skip_locked else ''}",
            (ids,),
        ).fetchall()
    ]
    if not locked:
        return 0
    available = _STORED.format(job="jobs")
    return conn.execute(
        f"UPDATE jobs SET available = {available} "
        f"WHERE id = ANY(%s) AND available IS DISTINCT FROM {available}",
        (locked,),
    ).rowcount


def refresh_available(ids: list[int]) -> int:
    """jobs.available for rows whose observations just changed, in batches of
    their own transaction (_BATCH), so a source's first pull does not hold
    thousands of row locks against the other pulls."""
    written = 0
    for start in range(0, len(ids), _BATCH):
        with transaction(), connection() as conn:
            written += _refresh_available(conn, ids[start : start + _BATCH], skip_locked=False)
    return written


def reconcile_available() -> int:
    """jobs.available over the whole catalog, in id chunks of their own
    transaction: what observations do not announce, a source switched on or
    off and the title pattern setting flipped, lands here within the hour,
    in the hourly retire_switched_off task. Only rows that differ are locked,
    and rows a concurrent writer holds are skipped for the next run, so it
    cannot deadlock against an ingest. Safe to stop and rerun. Returns the
    rows written."""
    available = _STORED.format(job="j")
    with pool.connection() as conn:
        bounds = conn.execute("SELECT min(id) AS lo, max(id) AS hi FROM jobs").fetchone()
    if not bounds or bounds["lo"] is None:
        return 0
    written = 0
    for lo in range(bounds["lo"], bounds["hi"] + 1, _RECONCILE_CHUNK):
        with transaction(), connection() as conn:
            stale = [
                r["id"]
                for r in conn.execute(
                    f"SELECT j.id FROM jobs j WHERE j.id >= %s AND j.id < %s "
                    f"AND j.available IS DISTINCT FROM {available}",
                    (lo, lo + _RECONCILE_CHUNK),
                ).fetchall()
            ]
            written += _refresh_available(conn, stale, skip_locked=True)
    return written


def observe(
    source: str,
    run_id: int | None,
    postings: list[JobPosting],
    kept: set[str],
    absence: str | None,
) -> dict[str, int]:
    """Appends to source_observations what this pull says about each catalog
    row that differs from the source's latest observation of it, and nothing
    for a row it says the same about. Call after upsert_postings, so a row
    this pull created has an id.

    `postings` is everything the board listed, admitted or not, and `kept`
    the urls its title pattern matched, whatever the enforcement switch says:
    the switch is applied when availability is read, so turning it over does
    not rewrite history. A listed posting whose record carries the feed's
    inactive flag is unlisted. `absence` is the kind a listed-before row the
    pull left out becomes: 'unlisted' after a complete pull of an
    authoritative board, 'not_listed' after an aggregator's, None when the
    pull cannot show it saw everything (partial or empty), which records no
    absence. A posting no catalog row holds has nothing to point at and is
    skipped; listings keeps it. Returns the count per kind."""
    by_url = {p.url: p for p in postings if p.url}
    with pool.connection() as conn:
        latest = {
            r["job_id"]: r["kind"]
            for r in conn.execute(
                "SELECT DISTINCT ON (job_id) job_id, kind FROM source_observations "
                "WHERE source = %s ORDER BY job_id, id DESC",
                (source,),
            ).fetchall()
        }
        listed = conn.execute(
            "SELECT id, url FROM jobs WHERE url = ANY(%s)", (list(by_url),)
        ).fetchall()
        rows: list[tuple[str, int, str]] = []
        for job in listed:
            posting = by_url[job["url"]]
            was = latest.pop(job["id"], None)
            if not posting.active:
                now = "unlisted"
            elif job["url"] not in kept:
                now = "filtered"
            elif was is None:
                now = "appeared"
            elif was in _ADMITTED:
                continue
            else:
                now = "reappeared"
            if now != was:
                rows.append((job["url"], job["id"], now))
        if absence == "unlisted":
            # A row this board owns that it has never been observed listing
            # (stored from the frozen feed flag, _STORED) is dropped by this
            # complete pull like any other: record it, or nothing would ever
            # close it. Only rows stored available, so the closed history is
            # not written out again.
            rows += [
                (r["url"], r["id"], absence)
                for r in conn.execute(
                    "SELECT j.id, j.url FROM jobs j WHERE j.source = %s AND j.available "
                    "AND j.url <> ALL(%s) AND NOT EXISTS (SELECT 1 FROM source_observations o "
                    "WHERE o.job_id = j.id AND o.source = %s)",
                    (source, list(by_url), source),
                ).fetchall()
            ]
        if absence:
            gone = [
                job_id for job_id, was in latest.items() if was in _ADMITTED or was == "filtered"
            ]
            if gone:
                rows += [
                    (r["url"], r["id"], absence)
                    for r in conn.execute(
                        "SELECT id, url FROM jobs WHERE id = ANY(%s)", (gone,)
                    ).fetchall()
                ]
    # The foreign key share-locks each job row, so the inserts take them in
    # url order (_LOCK_ORDER), in one transaction of their own.
    rows.sort(key=lambda r: r[0])
    if rows:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source_observations (job_id, source, kind, run_id) "
                "VALUES (%s, %s, %s, %s)",
                [(job_id, source, kind, run_id) for _, job_id, kind in rows],
            )
        # After the insert commits, so the refresh's snapshot sees it.
        refresh_available(sorted({job_id for _, job_id, _ in rows}))
    return dict(Counter(kind for _, _, kind in rows))


_BATCH = 500


# The one order every multi-row write to jobs and listings takes its row locks
# in: url, compared by code point. Two writers that lock overlapping rows in
# the same order cannot deadlock; two that each lock in their own order (a
# board's listing order, a scan's heap order) can, and did: 36 ingests failed
# on a deadlock in the 60 days to 2026-10-04, 32 of them inserting into
# listings, whose upsert ran in board order. Python sorts str by code point,
# and "C" collation compares UTF-8 bytes, which order the same way, so a
# sorted executemany and an ORDER BY with this collation agree.
_LOCK_ORDER: LiteralString = 'COLLATE "C"'


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


# Whether the value an incoming row {e} carries for a field equals what the
# stored row {l} holds, in either shape (core.listing_payloads). A digest is
# trusted only where it is written with its reference, so an inline value is
# compared as itself: no writer of an inline value keeps a digest beside it.
_KEPT: dict[str, LiteralString] = {
    "description": (
        "({e}.description_sha256 = {l}.description_sha256"
        # Empty: the listing did not carry the text, and keeps the stored one.
        " OR ({e}.description_sha256 IS NULL AND {e}.description = '')"
        " OR ({e}.description_sha256 IS NULL AND {l}.description_sha256 IS NULL"
        " AND {e}.description = {l}.description))"
    ),
    "raw": (
        "({e}.raw_sha256 = {l}.raw_sha256"
        " OR ({e}.raw_sha256 IS NULL AND {l}.raw_sha256 IS NULL AND {e}.raw = {l}.raw))"
    ),
}


def _kept(field: str, incoming: LiteralString, stored: LiteralString) -> LiteralString:
    return _KEPT[field].format(e=incoming, l=stored)


def _set_payload(field: LiteralString) -> LiteralString:
    """A field's four columns move together: all from the stored row when its
    value is kept, so a TOASTed inline value keeps its own pointer, otherwise
    all from the incoming row, so no reference outlives its value."""
    kept = _kept(field, "EXCLUDED", "listings")
    return ",\n".join(
        f"{column} = CASE WHEN {kept} THEN listings.{column} ELSE EXCLUDED.{column} END"
        for column in (field, f"{field}_sha256", f"{field}_object", f"{field}_object_size")
    )


def title_pattern_id(pattern: str) -> int:
    """The id of this exact pattern text in title_patterns, adding it if new.
    The select is its own statement so that it sees a row a concurrent pull
    inserted while this insert waited on the unique digest. A digest match
    with different text is refused rather than pointing a listing at another
    pattern."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO title_patterns (digest, pattern) "
            "VALUES (sha256(convert_to(%(p)s, 'UTF8')), %(p)s) ON CONFLICT (digest) DO NOTHING",
            {"p": pattern},
        )
        row = conn.execute(
            "SELECT id FROM title_patterns "
            "WHERE digest = sha256(convert_to(%(p)s, 'UTF8')) AND pattern = %(p)s",
            {"p": pattern},
        ).fetchone()
    if row is None:
        raise RuntimeError("title_patterns digest does not identify its exact pattern")
    return row["id"]


# The NOT EXISTS is the update's own SET, evaluated against the row: what the
# row would hold after it equals what it holds now. date_posted keeps the first
# date seen, so a board that dates by age ("Posted 3 Days Ago", Workday and the
# markdown lists), which yields a new timestamp on every pull, is not a change.
# The pattern is compared by pattern_id, which names exactly one text.
_RECORD_LISTINGS: LiteralString = f"""
    INSERT INTO listings
        (url, source, company, title, locations, date_posted, pattern_id, kept,
         description, description_sha256, description_object, description_object_size,
         raw, raw_sha256, raw_object, raw_object_size)
    SELECT v.url, v.source, v.company, v.title, v.locations, v.date_posted,
           v.pattern_id, v.kept,
           v.description, v.description_sha256, v.description_object, v.description_object_size,
           v.raw, v.raw_sha256, v.raw_object, v.raw_object_size
    FROM (VALUES (%s::text, %s::text, %s::text, %s::text, %s::text[],
                  %s::timestamptz, %s::bigint, %s::boolean,
                  %s::text, %s::bytea, %s::bytea, %s::integer,
                  %s::jsonb, %s::bytea, %s::bytea, %s::integer,
                  %s::integer))
        AS v (url, source, company, title, locations, date_posted, pattern_id, kept,
              description, description_sha256, description_object, description_object_size,
              raw, raw_sha256, raw_object, raw_object_size, refresh_hours)
    WHERE NOT EXISTS (
        SELECT 1 FROM listings l
        WHERE l.url = v.url
          AND (l.source, l.company, l.title, l.locations, l.pattern_id, l.kept)
              IS NOT DISTINCT FROM
              (v.source, v.company, v.title, v.locations, v.pattern_id, v.kept)
          AND (l.date_posted IS NOT NULL OR v.date_posted IS NULL)
          AND {_kept("description", "v", "l")}
          AND {_kept("raw", "v", "l")}
          AND l.last_seen_at >= now() - make_interval(hours => v.refresh_hours)
    )
    ON CONFLICT (url) DO UPDATE SET
        source = EXCLUDED.source, company = EXCLUDED.company,
        title = EXCLUDED.title, locations = EXCLUDED.locations,
        date_posted = COALESCE(listings.date_posted, EXCLUDED.date_posted),
        pattern_id = EXCLUDED.pattern_id, kept = EXCLUDED.kept,
        {_set_payload("description")},
        {_set_payload("raw")},
        last_seen_at = now()
"""


def record_listings(
    postings: list[JobPosting],
    source: str,
    pattern: str,
    kept: set[str],
    refresh_hours: int,
) -> int:
    """Keeps everything a board listed: the title (so a candidate pattern is
    judged against every title the board has listed), the posting text when the
    listing call carried it (so a backfill never scrapes a page the board
    already handed over), and the raw record minus that text (so a backtest
    can read a field nobody mapped). `kept` names the URLs the stored title
    pattern matched, including while pattern enforcement is disabled and
    every posting enters the catalog. Read by the admin source screens
    (pattern preview, screened postings), never by visibility or the checks;
    those read neither the text nor the raw record, and anything that does
    reads them through core.listing_payloads.resolve.

    A row is rewritten only when what it would hold differs, or when its
    last_seen_at is older than refresh_hours. Rewriting every listed row on
    every pull was 9.17 GB of WAL a day, 19% of the cluster's, for rows
    99.2% unchanged (pg_stat_statements over 24.6 hours, 2026-10-03). The
    unchanged rows are filtered out before the insert rather than by a
    WHERE on DO UPDATE, because DO UPDATE locks the conflicting row even
    when its WHERE refuses the update, and the lock is itself a logged page
    write: 1,174 bytes of WAL a row against 0 for the filter, measured on
    3,000 rows after a checkpoint.

    A row the board stopped listing is kept, like every row: all data is
    retained. A reader that means "listed now" says so with LISTED_NOW.

    The text and the raw record are written by reference to verified bundle
    objects uploaded before the upsert (listing_payloads.reference_columns).
    Returns how many values it had to write inline because object storage
    was unavailable."""
    listed = [p for p in postings if p.url]
    if in_transaction():
        raise RuntimeError("Listings upload their payloads and cannot record inside a transaction")
    payloads, inline = listing_payloads.reference_columns(
        [(p.url, p.description or "", p.raw or {}) for p in listed]
    )
    pattern_id = title_pattern_id(pattern)
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
            pattern_id,
            p.url in kept,
            *payload[:4],
            Jsonb(payload[4]),
            *payload[5:],
            refresh_hours,
        )
        for p, payload in zip(listed, payloads, strict=True)
    ]
    # In url order, because boards share urls and the upsert locks each row
    # it writes until COMMIT (_LOCK_ORDER).
    rows.sort(key=lambda r: r[0])
    if rows:
        with pool.connection() as conn, conn.cursor() as cur:
            # description and raw are set from the old row when equal, because
            # Postgres reuses a TOASTed value only when handed the old row's own
            # pointer; a value from EXCLUDED is a fresh copy, written out again
            # chunk by chunk.
            cur.executemany(_RECORD_LISTINGS, rows)
    return inline
