"""Source observations and the stored availability projection."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, LiteralString

from core.pool import connection, pool, transaction

if TYPE_CHECKING:
    from core.fetching.posting import JobPosting

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
    posting_by_url = {posting.url: posting for posting in postings if posting.url}
    with pool.connection() as conn:
        last_observation_by_job = {
            observation["job_id"]: observation["kind"]
            for observation in conn.execute(
                "SELECT DISTINCT ON (job_id) job_id, kind FROM source_observations "
                "WHERE source = %s ORDER BY job_id, id DESC",
                (source,),
            ).fetchall()
        }
        listed_jobs = conn.execute(
            "SELECT id, url FROM jobs WHERE url = ANY(%s)", (list(posting_by_url),)
        ).fetchall()
        observation_rows: list[tuple[str, int, str]] = []
        for job in listed_jobs:
            posting = posting_by_url[job["url"]]
            previous_kind = last_observation_by_job.pop(job["id"], None)
            if not posting.active:
                observed_kind = "unlisted"
            elif job["url"] not in kept:
                observed_kind = "filtered"
            elif previous_kind is None:
                observed_kind = "appeared"
            elif previous_kind in _ADMITTED:
                continue
            else:
                observed_kind = "reappeared"
            if observed_kind != previous_kind:
                observation_rows.append((job["url"], job["id"], observed_kind))
        if absence == "unlisted":
            # A row this board owns that it has never been observed listing
            # (stored from the frozen feed flag, _STORED) is dropped by this
            # complete pull like any other: record it, or nothing would ever
            # close it. Only rows stored available, so the closed history is
            # not written out again.
            observation_rows += [
                (job["url"], job["id"], absence)
                for job in conn.execute(
                    "SELECT j.id, j.url FROM jobs j WHERE j.source = %s AND j.available "
                    "AND j.url <> ALL(%s) AND NOT EXISTS (SELECT 1 FROM source_observations o "
                    "WHERE o.job_id = j.id AND o.source = %s)",
                    (source, list(posting_by_url), source),
                ).fetchall()
            ]
        if absence:
            missing_job_ids = [
                job_id
                for job_id, previous_kind in last_observation_by_job.items()
                if previous_kind in _ADMITTED or previous_kind == "filtered"
            ]
            if missing_job_ids:
                observation_rows += [
                    (job["url"], job["id"], absence)
                    for job in conn.execute(
                        "SELECT id, url FROM jobs WHERE id = ANY(%s)", (missing_job_ids,)
                    ).fetchall()
                ]
    # The foreign key share-locks each job row, so the inserts take them in
    # url order (_LOCK_ORDER), in one transaction of their own.
    observation_rows.sort(key=lambda row: row[0])
    if observation_rows:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source_observations (job_id, source, kind, run_id) "
                "VALUES (%s, %s, %s, %s)",
                [(job_id, source, kind, run_id) for _, job_id, kind in observation_rows],
            )
        # After the insert commits, so the refresh's snapshot sees it.
        refresh_available(sorted({job_id for _, job_id, _ in observation_rows}))
    return dict(Counter(kind for _, _, kind in observation_rows))


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
