"""Retained listing evidence and payload-reference writes."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, LiteralString

from psycopg.types.json import Jsonb

from core.payloads import listings
from core.pool import in_transaction, pool

if TYPE_CHECKING:
    from core.fetching.posting import JobPosting

# Whether the value an incoming row {e} carries for a field equals what the
# stored row {l} holds, in either shape (core.payloads.listings). A digest is
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
    reads them through core.payloads.listings.resolve.

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
    objects uploaded before the upsert (listings.reference_columns).
    Returns how many values it had to write inline because object storage
    was unavailable."""
    listed_postings = [posting for posting in postings if posting.url]
    if in_transaction():
        raise RuntimeError("Listings upload their payloads and cannot record inside a transaction")
    payloads, inline_payloads = listings.reference_columns(
        [(posting.url, posting.description or "", posting.raw or {}) for posting in listed_postings]
    )
    pattern_id = title_pattern_id(pattern)
    listing_rows = [
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
        for p, payload in zip(listed_postings, payloads, strict=True)
    ]
    # In url order, because boards share urls and the upsert locks each row
    # it writes until COMMIT (_LOCK_ORDER).
    listing_rows.sort(key=lambda row: row[0])
    if listing_rows:
        with pool.connection() as conn, conn.cursor() as cur:
            # description and raw are set from the old row when equal, because
            # Postgres reuses a TOASTed value only when handed the old row's own
            # pointer; a value from EXCLUDED is a fresh copy, written out again
            # chunk by chunk.
            cur.executemany(_RECORD_LISTINGS, listing_rows)
    return inline_payloads
