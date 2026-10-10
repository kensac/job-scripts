from __future__ import annotations

import datetime
import logging
import random
import time
from collections import Counter
from typing import TYPE_CHECKING, LiteralString

from psycopg import errors
from psycopg.types.json import Jsonb

from core import listing_payloads
from core.pool import in_transaction, pool

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
        # Locked in url order (_LOCK_ORDER) before the update, as every
        # multi-row writer of jobs and listings does; a bare UPDATE locks in
        # scan order and deadlocks against another board's upsert of a shared
        # url.
        dropped = conn.execute(
            "UPDATE jobs SET active = false WHERE id IN ("
            "  SELECT id FROM jobs WHERE source = %s AND active AND url <> ALL(%s) "
            f"  ORDER BY url {_LOCK_ORDER} FOR UPDATE) "
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


def retire_switched_off(patterns_enforced: bool) -> dict[str, int]:
    """Marks inactive every active row whose source is switched off, unless a
    switched-on source lists the url and would admit it. A switched-off source
    is never pulled, so nothing else ever retires its rows: on 2026-10-04
    production held 50,994 active rows of 115 switched-off sources, 24,736 of
    them sr_domino_s. The exception is a url whose listings row belongs to a
    source that is on, kept by its pattern or admitted because enforcement is
    off: that source's next pull would put it back, and every return queues a
    re-check (371 rows on that day). Re-enabled, the source's own pull
    reactivates its rows through upsert_postings, so this is reversible.

    Runs every cycle and is a no-op once the catalog agrees, which is what
    reaches every way a source is switched off: the sources page, a bundle
    switch, the automatic switch-off of a board that keeps failing, or a
    direct write. Rows a concurrent upsert holds are skipped, not waited on,
    and the next cycle takes them, so it cannot deadlock against an ingest.
    It still locks in url order (_LOCK_ORDER), as every multi-row writer of
    jobs does. Returns the count per source."""
    with pool.connection() as conn:
        rows = conn.execute(
            f"""
            WITH doomed AS (
                SELECT j.id FROM jobs j JOIN sources s ON s.name = j.source AND NOT s.active
                WHERE j.active AND NOT EXISTS (
                    SELECT 1 FROM listings l JOIN sources o ON o.name = l.source AND o.active
                    WHERE l.url = j.url AND (l.kept OR NOT %(enforced)s))
                ORDER BY j.url {_LOCK_ORDER} FOR UPDATE OF j SKIP LOCKED
            ),
            retired AS (
                UPDATE jobs SET active = false FROM doomed
                WHERE jobs.id = doomed.id AND jobs.active
                RETURNING jobs.id, jobs.source
            ),
            logged AS (
                INSERT INTO job_listing_events (job_id, source, listed)
                SELECT id, source, false FROM retired
            )
            SELECT source, count(*) AS n FROM retired GROUP BY source
            """,
            {"enforced": patterns_enforced},
        ).fetchall()
    return {r["source"]: r["n"] for r in rows}


_ADMITTED = frozenset({"appeared", "reappeared"})


# Whether job {job} is available, as source_observations say: true when some
# switched-on source's latest observation of it admits it, false when it has
# observations and none of them do, NULL (cannot tell) when no source has
# observed it. not_listed is aggregator absence, which never closes anything;
# filtered admits only while title patterns are not enforced, the same rule
# retire_switched_off applies to listings.kept. One definition, for the
# shadow comparison now and for the readers of jobs.active after cutover.
AVAILABLE: LiteralString = """(
    CASE WHEN EXISTS (
            SELECT 1 FROM (
                SELECT DISTINCT ON (o.source) o.source, o.kind FROM source_observations o
                WHERE o.job_id = {job}.id ORDER BY o.source, o.id DESC) latest
            JOIN sources s ON s.name = latest.source AND s.active
            WHERE latest.kind IN ('appeared', 'reappeared', 'not_listed')
               OR (latest.kind = 'filtered' AND NOT %(enforced)s))
         THEN true
         WHEN EXISTS (SELECT 1 FROM source_observations o WHERE o.job_id = {job}.id)
         THEN false
    END)"""


def availability_shadow(patterns_enforced: bool) -> list[dict]:
    """jobs.active against AVAILABLE over the whole catalog, in one snapshot:
    a count per (legacy, projected, owning source) with up to three example
    job ids. Read-only. Run beside retire_switched_off until the readers of
    jobs.active move, so each cycle leaves one comparison on its task."""
    available = AVAILABLE.format(job="j")
    with pool.connection() as conn:
        return conn.execute(
            f"""
            SELECT legacy, projected, source, count(*) AS n,
                   (array_agg(id ORDER BY id))[1:3] AS examples
            FROM (SELECT j.id, j.source, j.active AS legacy, {available} AS projected
                  FROM jobs j) p
            GROUP BY legacy, projected, source
            """,
            {"enforced": patterns_enforced},
        ).fetchall()


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
# The pattern is compared by pattern_id, which names exactly one text, so a row
# written before pattern_id existed (NULL) is a change and gains one on the
# first pull that still lists it. Any rewrite empties the copy in `pattern`,
# which nothing reads; it is dropped once empty (migrations.md).
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
        pattern = NULL, pattern_id = EXCLUDED.pattern_id, kept = EXCLUDED.kept,
        {_set_payload("description")},
        {_set_payload("raw")},
        last_seen_at = now()
"""


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

    Rows the board stopped listing age out retention_days after the pull
    that last listed them, counted from last_seen_at plus refresh_hours
    because last_seen_at can lag that pull by up to refresh_hours. So a row
    is never deleted sooner than before and at most refresh_hours later. A
    row still listed is never deleted: the pull refreshes it first.

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
    # Its own transaction, locking in the same order. Inside the upsert's it
    # was a second ascending pass after the first, which is not one order:
    # it waited on a stale row another board was upserting while holding
    # rows that board would reach next. It deletes only rows no pull has
    # listed in retention_days, so nothing needs it atomic with the upsert.
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM listings WHERE url IN ("
            "  SELECT url FROM listings WHERE source = %s "
            "  AND last_seen_at < now() - make_interval(days => %s, hours => %s) "
            f"  ORDER BY url {_LOCK_ORDER} FOR UPDATE)",
            (source, retention_days, refresh_hours),
        )
    return inline


def listings_holding_a_pattern_copy() -> dict[str, int]:
    """Rows that still carry their pattern's text in listings.pattern, by
    source, after storing every such text in title_patterns. A pull empties a
    row it rewrites; these are what no pull has rewritten since, most of them
    rows no board lists any more, kept for retention. One scan of the table
    for each half."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO title_patterns (digest, pattern) "
            "SELECT DISTINCT sha256(convert_to(pattern, 'UTF8')), pattern FROM listings "
            "WHERE pattern IS NOT NULL "
            "ON CONFLICT (digest) DO NOTHING"
        )
        rows = conn.execute(
            "SELECT source, count(*) AS n FROM listings WHERE pattern IS NOT NULL GROUP BY source"
        ).fetchall()
    return {r["source"]: r["n"] for r in rows}


def drop_pattern_copies(source: str, limit: int = _BATCH) -> int:
    """For up to `limit` of this source's rows that still carry the text,
    points pattern_id at the stored copy of that text and empties the column.
    The text is what the row's last writer meant, so it decides the pointer,
    and the row is only emptied when its text is stored. Locked through the
    source index in url order (_LOCK_ORDER), as the pull's upsert and the
    retention delete lock them. Returns how many rows it changed; 0 means
    none of this source's are left whose text listings_holding_a_pattern_copy
    stored."""
    with pool.connection() as conn:
        return conn.execute(
            "UPDATE listings l SET pattern_id = t.id, pattern = NULL FROM title_patterns t "
            "WHERE l.url IN ("
            "  SELECT url FROM listings WHERE source = %s AND pattern IS NOT NULL "
            f"  ORDER BY url {_LOCK_ORDER} LIMIT %s FOR UPDATE) "
            "AND t.pattern = l.pattern",
            (source, limit),
        ).rowcount
