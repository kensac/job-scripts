"""Per-job signals: possibilities, not facts.

Each signal is a claim plus the evidence and sample size it rests on. A signal
that cannot clear its floor is ABSENT from the result rather than degraded into
a hedge - the caller renders a missing key as nothing at all, so silence is a
correct output and a pane with two honest signals beats one with six guesses.

Nothing here predicts. "23 of 4,700 postings from this board were already gone
the first time we checked" is an observation; "this posting will close soon" is
not, and at these sample sizes it would be a lie. The floors below are what
keeps the difference enforceable rather than editorial.

Two columns are deliberately unused. `jobs.active` is whatever a board's feed
last said and nothing ever clears it, so it measures whether a board still
LISTS a posting, not whether the posting is open. `jobs.created_at` is a
catalog-load timestamp bounded below by the last reseed, so any "first seen"
derived from it would be an artefact of a database rebuild. Age comes from
date_posted and openness comes from the closed-check.
"""

from __future__ import annotations

import datetime
import time
from typing import Any

from api import db

# A proportion needs enough trials before it carries information. Thirty is the
# conventional floor for the normal approximation to the binomial: below it a
# single extra rejection moves the rate by whole percentage points. Every board
# that survives this floor has 100+ first-checks, so it excludes only `upload`
# (n=1) and boards that have barely been swept.
BOARD_RELIABILITY_MIN_CHECKED = 30

# A repost claim needs at least two postings to be about anything.
REPOST_MIN_URLS = 2

# Below a fortnight, "the same role posted twice" is not employer behaviour.
# Measured over the catalog: of 4,848 same-source (company, title) groups
# carrying more than one url, 1,838 (38%) share a single day and the median
# span is 4.7 days. Same-day duplicates are not reposts - and the obvious
# explanation fails, since differing locations appear in 42% of same-day groups
# against 49% of separated ones, so location does not account for them. Cutting
# at fourteen days drops that mass and leaves 1,815 groups where the gap is
# long enough that a person re-listed something.
REPOST_MIN_SPAN_DAYS = 14

# Board reliability is cumulative over a board's whole history (7,159
# first-checks for the largest), and new verdicts arrive on the hourly ingest
# cycle, so an hour of fresh checks cannot move it by a tenth of a point.
# Recomputing it per drawer-open costs 178ms for a number identical across all
# 18,888 postings from that board; fifteen minutes bounds staleness well inside
# the cadence that produces it.
_BOARD_RELIABILITY_TTL_SECONDS = 900

_DAY = 86400.0


def first_closed_sql(*, one_source: bool = False) -> str:
    """Dead on arrival: the FIRST closed-check ever run on a posting already
    said closed. Ordering by id ASC rather than DESC is the whole difference
    from the latest-verdict funnel - it asks whether the board handed us
    something already dead, not whether the posting has died since.

    One definition, two callers: the per-board analytics endpoint groups it
    across every source, this module reads a single board. A second copy is
    how the two would drift.
    """
    where = "AND j.source = %(source)s" if one_source else ""
    group = "" if one_source else "GROUP BY source"
    select = "" if one_source else "source, "
    return f"""
WITH q AS (
    SELECT j.source AS source, j.id AS job_id, a.status, a.id AS qid
    FROM ai_queries a
    JOIN jobs j ON j.url = a.url
    WHERE a.check_type = 'closed' AND a.status IN ('passed', 'rejected') {where}
), firsts AS (
    SELECT DISTINCT ON (job_id) source, status FROM q ORDER BY job_id, qid ASC
)
SELECT {select}
       count(*) AS first_checked,
       count(*) FILTER (WHERE status = 'rejected') AS dead_on_arrival
FROM firsts {group}
"""


# Process-local, and that is enough: this is a display statistic, so three
# hosts holding independently-aged copies is not a correctness problem. Two
# threads racing to fill the same key recompute the same value and one wins,
# which is why no lock is taken - a dict get/set needs none, and blocking a
# request thread to save a duplicate query would be the worse trade.
_board_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _board_reliability(source: str) -> dict[str, Any] | None:
    cached = _board_cache.get(source)
    now = time.monotonic()
    if cached and now - cached[0] < _BOARD_RELIABILITY_TTL_SECONDS:
        return cached[1]
    row = db.query_one(first_closed_sql(one_source=True), {"source": source})
    checked = int(row["first_checked"]) if row else 0
    signal = (
        None
        if checked < BOARD_RELIABILITY_MIN_CHECKED
        else {
            "source": source,
            "dead_on_arrival": int(row["dead_on_arrival"]),  # pyright: ignore[reportOptionalSubscript]
            "sample_n": checked,
        }
    )
    _board_cache[source] = (now, signal)
    return signal


def _posting_age(job: dict[str, Any]) -> dict[str, Any] | None:
    """Purely observational, so it carries no sample size - it is a fact about
    this one listing. Absent when the board supplied no date (sheet_import
    carries one for 1,628 of 6,021 postings, upload for none), because the
    only fallback would be the reseed-bounded created_at."""
    posted = job.get("date_posted")
    if not isinstance(posted, datetime.datetime):
        return None
    days = (datetime.datetime.now(tz=datetime.UTC) - posted).total_seconds() / _DAY
    if days < 0:
        # A future date_posted is a feed error, not a posting from tomorrow.
        return None
    return {"posted_at": posted, "days_listed": int(days)}


# Location is part of the key, and leaving it out was a real defect rather
# than a nicety. Without it a chain listing one role across its estate reads as
# one enormous repost: Sainsbury's "Trading Assistant" grouped to 1,056 urls
# spanning 120 distinct locations and 107 days, which is bulk multi-site
# hiring and not an employer re-listing anything. Keying on location as well
# leaves 1,510 groups catalog-wide, 89% of them two to five urls.
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


def _repost(job: dict[str, Any]) -> dict[str, Any] | None:
    """Scoped to a single source on purpose. The same (company, title) under
    two urls from two different boards is our ingest seeing one posting twice,
    not an employer re-listing it: 15% of multi-url pairs span sources and are
    excluded here rather than hedged about in the wording.

    The match is casefold+trim on free text - there is no company or requisition
    identity in this schema - so the claim a caller may make is "this company
    name and title string recurred", never "this employer reposts". A large
    url_count in particular means an evergreen requisition rather than a
    repost cycle, and the two are not separable here: the count is reported so
    a reader can tell them apart, not so it can be characterised.
    """
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
    return {
        "title": job["title"],
        "url_count": int(row["url_count"]),
        "first_posted_at": row["first_posted_at"],
        "last_posted_at": row["last_posted_at"],
        "span_days": span_days,
    }


def signals_for(job: dict[str, Any]) -> dict[str, Any]:
    """Every key is optional. A caller must treat a missing key as "this signal
    does not exist" - never as a zero, and never as something to re-derive.
    """
    built = {
        "posting_age": _posting_age(job),
        "board_reliability": _board_reliability(job["source"]),
        "repost": _repost(job),
    }
    return {name: signal for name, signal in built.items() if signal is not None}
