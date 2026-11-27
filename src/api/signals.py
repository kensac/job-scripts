"""Per-job observational signals.

Each signal is optional and returns None below its sample floor. Age comes from
`date_posted` (not `jobs.created_at`, which reflects catalog reloads) and
openness comes from the closed check (not frozen `jobs.active`).
"""

from __future__ import annotations

import datetime
import time
from typing import Any

from pydantic import BaseModel

from api import db

# Minimum first-check sample size before reporting a board's dead-on-arrival rate.
BOARD_RELIABILITY_MIN_CHECKED = 30

REPOST_MIN_URLS = 2

# Drops same-day multi-URL duplicates (median span 4.7 days in catalog) so only
# roles re-listed across at least two weeks surface as reposts.
REPOST_MIN_SPAN_DAYS = 14

# Cached per process; verdicts arrive hourly so 15 minutes bounds staleness.
_BOARD_RELIABILITY_TTL_SECONDS = 900

_DAY = 86400.0


def first_closed_sql(*, one_source: bool = False) -> str:
    """Count first-ever closed checks (id ASC) per source or for one source.

    Shared by board analytics and per-job signals so both agree on dead-on-arrival rates.
    """
    where = "AND j.source = %(source)s" if one_source else ""
    group = "" if one_source else "GROUP BY source"
    select = "" if one_source else "source, "
    return f"""
WITH q AS (
    SELECT j.source AS source, j.id AS job_id, a.status, a.id AS qid
    FROM verdicts a
    JOIN jobs j ON j.url = a.url
    WHERE a.check_type = 'closed' {where}
), firsts AS (
    SELECT DISTINCT ON (job_id) source, status FROM q ORDER BY job_id, qid ASC
)
SELECT {select}
       count(*) AS first_checked,
       count(*) FILTER (WHERE status = 'rejected') AS dead_on_arrival
FROM firsts {group}
"""


_board_cache: dict[str, tuple[float, BoardReliability | None]] = {}


class PostingAge(BaseModel):
    """How long this one listing has been up. Carries no sample size, because
    it is a fact about this posting rather than a rate over many."""

    posted_at: datetime.datetime
    days_listed: int


class BoardReliability(BaseModel):
    """How often this board's postings were already gone the first time they
    were checked. An observation over `sample_n`, not a prediction."""

    source: str
    dead_on_arrival: int
    sample_n: int


class Repost(BaseModel):
    """The same title, at the same locations, listed more than once. Location
    is part of the key: without it one chain listing a role across its estate
    reads as one enormous repost."""

    title: str | None
    url_count: int
    first_posted_at: datetime.datetime | None
    last_posted_at: datetime.datetime | None
    span_days: int | None


class Signals(BaseModel):
    """Every signal is null when it cannot clear its floor.

    It used to be absent instead, because a caller renders "no signal" as
    nothing at all and an empty object is a correct answer. Declaring the
    shape made the keys appear with null, which the one consumer reads
    identically: `!signals?.posting_age` is true either way. Null is kept
    rather than serialised away because a key that is sometimes absent cannot
    be typed, and being able to type it is the reason this exists.

    What must not change is that a null here means the signal DOES NOT EXIST.
    Never a zero, and never something for the caller to re-derive."""

    posting_age: PostingAge | None = None
    board_reliability: BoardReliability | None = None
    repost: Repost | None = None


def _board_reliability(source: str) -> BoardReliability | None:
    cached = _board_cache.get(source)
    now = time.monotonic()
    if cached and now - cached[0] < _BOARD_RELIABILITY_TTL_SECONDS:
        return cached[1]
    row = db.query_one(first_closed_sql(one_source=True), {"source": source})
    checked = int(row["first_checked"]) if row else 0
    signal = (
        None
        if checked < BOARD_RELIABILITY_MIN_CHECKED
        else BoardReliability(
            source=source,
            dead_on_arrival=int(row["dead_on_arrival"]),  # pyright: ignore[reportOptionalSubscript]
            sample_n=checked,
        )
    )
    _board_cache[source] = (now, signal)
    return signal


def _posting_age(job: dict[str, Any]) -> PostingAge | None:
    """Days since `date_posted`, or None when missing or in the future."""
    posted = job.get("date_posted")
    if not isinstance(posted, datetime.datetime):
        return None
    days = (datetime.datetime.now(tz=datetime.UTC) - posted).total_seconds() / _DAY
    if days < 0:
        return None
    return PostingAge(posted_at=posted, days_listed=int(days))


# Keyed by (source, company, title, locations) so multi-location hiring and
# cross-board mirrors are not counted as reposts.
_REPOST_SQL = """
SELECT count(*) AS url_count,
       min(date_posted) AS first_posted_at,
       max(date_posted) AS last_posted_at
FROM jobs
WHERE source = %(source)s
  AND lower(btrim(company)) = %(company)s
  AND lower(btrim(title)) = %(title)s
  AND locations = %(locations)s
  AND date_posted IS NOT NULL
"""


def _repost(job: dict[str, Any]) -> Repost | None:
    """Same-source, same-location recurring `(company, title)` across at least
    `REPOST_MIN_URLS` URLs and `REPOST_MIN_SPAN_DAYS` days."""
    company = (job.get("company") or "").strip().lower()
    title = (job.get("title") or "").strip().lower()
    if not company or not title:
        return None
    row = db.query_one(
        _REPOST_SQL,
        {
            "source": job["source"],
            "company": company,
            "title": title,
            "locations": job.get("locations") or [],
        },
    )
    if not row or int(row["url_count"]) < REPOST_MIN_URLS:
        return None
    span_days = int((row["last_posted_at"] - row["first_posted_at"]).total_seconds() / _DAY)
    if span_days < REPOST_MIN_SPAN_DAYS:
        return None
    return Repost(
        title=job["title"],
        url_count=int(row["url_count"]),
        first_posted_at=row["first_posted_at"],
        last_posted_at=row["last_posted_at"],
        span_days=span_days,
    )


def signals_for(job: dict[str, Any]) -> Signals:
    """Compute optional per-job signals; None means unavailable or below floor."""
    return Signals(
        posting_age=_posting_age(job),
        board_reliability=_board_reliability(job["source"]),
        repost=_repost(job),
    )
