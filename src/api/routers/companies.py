"""The company view over the posting catalog.

Keyed on a NAME, not on a company. `jobs.company` is free text extracted per
posting; there is no company table, id or slug, and `company_key` is
lower(btrim(company)) and nothing more. So "Stripe" and "Stripe, Inc." are two
rows here, postings are undercounted per employer and application history is
fragmented across spellings. A distinct-name count is an UPPER BOUND on the
number of real companies. Resolving that is an entity-resolution project
rather than a field, so nothing here claims to have done it.

Two fields are deliberately absent. There is no closure or time-to-close: only
162 names have any observed closure and four reach n>=10, so a per-company
median would be noise with a decimal point - and the earlier attempt at one
measured feed staleness rather than posting lifespan while reading as a clean
five-day figure. There is no "roles currently running" either, because the
only column that could supply it is `jobs.active`, which reports whether a
board still lists a posting rather than whether the role is open; sorting on
it would rank companies by which board happened to scrape them.

Anything below its sample floor is OMITTED. Not nulled, not zeroed, not
flagged - the server is the single authority on what is well powered, and a
flag a caller might forget to check puts that decision in two places.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api import db, signals
from api.auth import AuthedUser
from api.routers.admin import require_admin

router = APIRouter(prefix="/admin")

# A count is not a rate, so presence is the only threshold that applies: a
# single application under a name is a fact about that name, not a sample from
# which anything is extrapolated.
APPLICATIONS_MIN = 1

# The open share IS read as a proportion ("9 of 14 still answer"), but it is
# rendered as its own counts rather than extrapolated, so the binomial floor
# that governs a true rate is heavier than this needs. Five is the point below
# which the phrase stops being worth a sentence; it still omits the field for
# roughly 92% of names, since only 616 of 7,564 reach it.
OPEN_MIN_CHECKED = 5

_SORTABLE = {
    "company_name": "company_name",
    "total_postings_seen": "total_postings_seen",
    "comp_extracted_n": "comp_found",
    "applications": "applications_n",
}

_MAX_LIMIT = 200

# Grouping on lower(btrim(company)) rather than the raw string is the whole
# identity model, such as it is. mode() picks the most common spelling for
# display so the UI shows "Stripe" rather than whichever variant sorted first.
#
# The comp split is three-way on purpose. comp_extracted says the extractor
# ran; an amount says it found one. A posting where it ran and found nothing
# (3,000 of them) is the closest this schema gets to "the posting stated no
# pay" - but it still cannot separate that from "the posting stated pay and we
# missed it", so both live in one bucket named for what WE did.
_BASE_SQL = """
WITH base AS (
    SELECT lower(btrim(company)) AS company_key,
           mode() WITHIN GROUP (ORDER BY company) AS company_name,
           count(*) AS total_postings_seen,
           count(*) FILTER (
               WHERE comp_extracted AND (comp_min IS NOT NULL OR comp_max IS NOT NULL)
           ) AS comp_found,
           count(*) FILTER (
               WHERE comp_extracted AND comp_min IS NULL AND comp_max IS NULL
           ) AS comp_ran_found_nothing,
           count(*) FILTER (WHERE NOT comp_extracted) AS comp_not_attempted
    FROM jobs WHERE company <> ''
    GROUP BY lower(btrim(company))
), apps AS (
    SELECT lower(btrim(j.company)) AS company_key,
           count(*) AS applications_n,
           max(uj.date_applied) AS last_applied_at
    FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
    WHERE NOT uj.hidden AND j.company <> ''
    GROUP BY lower(btrim(j.company))
)
SELECT base.*,
       COALESCE(apps.applications_n, 0) AS applications_n,
       apps.last_applied_at
FROM base LEFT JOIN apps ON apps.company_key = base.company_key
WHERE (%(q)s::text IS NULL OR base.company_key LIKE %(q)s::text)
ORDER BY {order}, base.company_key
LIMIT %(limit)s OFFSET %(offset)s
"""

_COUNT_SQL = """
SELECT count(*) AS c FROM (
    SELECT lower(btrim(company)) AS company_key FROM jobs
    WHERE company <> '' GROUP BY lower(btrim(company))
) t WHERE (%(q)s::text IS NULL OR t.company_key LIKE %(q)s::text)
"""

# Currency is present on 452 of 11,442 extracted rows, so the NULL bucket is
# the overwhelming majority. It is reported as its own entry rather than
# folded into USD: an amount whose currency was never captured is not a dollar
# figure, and averaging across the two would invent one.
_CURRENCY_SQL = """
SELECT lower(btrim(company)) AS company_key, comp_currency AS currency,
       count(*) AS n, min(comp_min) AS min, max(comp_max) AS max
FROM jobs
WHERE company <> '' AND comp_extracted
  AND (comp_min IS NOT NULL OR comp_max IS NOT NULL)
  AND lower(btrim(company)) = ANY(%(keys)s)
GROUP BY lower(btrim(company)), comp_currency
"""

_STATUS_SQL = """
SELECT lower(btrim(j.company)) AS company_key, uj.status, count(*) AS n
FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
WHERE NOT uj.hidden AND j.company <> '' AND uj.status IS NOT NULL AND uj.status <> ''
  AND lower(btrim(j.company)) = ANY(%(keys)s)
GROUP BY lower(btrim(j.company)), uj.status
"""

# The same instrument the board row and the per-job signal use: the latest
# closed verdict per posting. Computing it any other way - "has any closed row
# in ai_queries", say, which would count 'failed' rows too - would let this
# page and the board disagree with nobody able to see which was wrong.
_OPEN_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (j.id) lower(btrim(j.company)) AS company_key,
           a.status, a.created_at
    FROM ai_queries a JOIN jobs j ON j.url = a.url
    WHERE a.check_type = 'closed' AND a.status IN ('passed', 'rejected')
      AND lower(btrim(j.company)) = ANY(%(keys)s)
    ORDER BY j.id, a.id DESC
)
SELECT company_key,
       count(*) AS n_checked,
       count(*) FILTER (WHERE status = 'passed') AS n_open,
       max(created_at) AS last_checked_at
FROM latest GROUP BY company_key
"""

# One title repeated under several urls on a SINGLE source AT ONE LOCATION,
# more than REPOST_MIN_SPAN_DAYS apart. Every exclusion is measured, not
# assumed, and the constants live in api.signals so the company page and the
# per-job pane cannot disagree about what a repost is.
#
# Location is in the key because without it a chain listing one role across its
# estate reads as one enormous repost - Sainsbury's "Trading Assistant"
# grouped to 1,056 urls over 120 locations, which is bulk hiring, not
# re-listing. It also picks the LARGEST surviving group per company, so a high
# url_count still means an evergreen requisition rather than a repost cycle;
# the count is reported so a reader can tell those apart.
_REPOST_SQL = """
WITH g AS (
    SELECT lower(btrim(company)) AS company_key,
           mode() WITHIN GROUP (ORDER BY title) AS title,
           count(*) AS url_count,
           min(date_posted) AS first_posted_at,
           max(date_posted) AS last_posted_at
    FROM jobs
    WHERE company <> '' AND title <> '' AND date_posted IS NOT NULL
      AND lower(btrim(company)) = ANY(%(keys)s)
    GROUP BY lower(btrim(company)), lower(btrim(title)), source, locations
    HAVING count(*) >= %(min_urls)s
       AND max(date_posted) - min(date_posted) > make_interval(days => %(min_span)s)
)
SELECT DISTINCT ON (company_key)
       company_key, title, url_count, first_posted_at, last_posted_at,
       (extract(epoch FROM last_posted_at - first_posted_at) / 86400)::int AS span_days
FROM g ORDER BY company_key, url_count DESC, last_posted_at DESC
"""

_CAVEATS = [
    "Rows are keyed on the company NAME as extracted from a posting, not on a "
    "resolved company. Spelling variants are separate rows, so a distinct-name "
    "count is an upper bound on the number of real companies.",
    "Compensation counts describe what our extractor found, not what an "
    "employer published: a posting that stated pay we failed to read is "
    "indistinguishable from one that stated none.",
    "Most extracted amounts carry no currency, so they are grouped under a "
    "null currency rather than assumed to be USD.",
    "There is no closure or time-to-close figure: too few postings per name "
    "have been observed closing for a median to mean anything.",
]


def _bucket(rows: list[dict[str, Any]], key: str = "company_key") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row[key], []).append(row)
    return out


def _item(
    base: dict[str, Any],
    *,
    currency: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    open_row: dict[str, Any] | None,
    repost: dict[str, Any] | None,
) -> dict[str, Any]:
    total = int(base["total_postings_seen"])
    item: dict[str, Any] = {
        "company_key": base["company_key"],
        "company_name": base["company_name"],
        "total_postings_seen": total,
        "comp": {
            "n_extracted": int(base["comp_found"]),
            "n_total": total,
            "by_currency": [
                {
                    "currency": row["currency"],
                    "n": int(row["n"]),
                    "min": int(row["min"]) if row["min"] is not None else None,
                    "max": int(row["max"]) if row["max"] is not None else None,
                }
                for row in sorted(currency, key=lambda r: -int(r["n"]))
            ],
        },
        # Named for what the extractor did, because that is all these three
        # can honestly claim. They sum to total_postings_seen.
        "extraction": {
            "found_pay": int(base["comp_found"]),
            "ran_found_nothing": int(base["comp_ran_found_nothing"]),
            "not_attempted": int(base["comp_not_attempted"]),
        },
    }
    applications_n = int(base["applications_n"])
    if applications_n >= APPLICATIONS_MIN:
        item["applications"] = {
            "n": applications_n,
            "last_applied_at": base["last_applied_at"],
            "statuses": {r["status"]: int(r["n"]) for r in statuses},
        }
    if open_row and int(open_row["n_checked"]) >= OPEN_MIN_CHECKED:
        item["open"] = {
            "n_open": int(open_row["n_open"]),
            "n_checked": int(open_row["n_checked"]),
            "last_checked_at": open_row["last_checked_at"],
        }
    if repost:
        item["repost"] = {
            "title": repost["title"],
            "url_count": int(repost["url_count"]),
            "first_posted_at": repost["first_posted_at"],
            "last_posted_at": repost["last_posted_at"],
            "span_days": int(repost["span_days"]),
        }
    return item


def _offset_from(cursor: str | None) -> int:
    """The cursor is an offset, and says so rather than pretending to be a
    keyset. Sorting is over a whole-table aggregate that has to be recomputed
    per page anyway, so a keyset would buy nothing and would break the moment
    the caller sorted on a non-unique column."""
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "BAD_CURSOR", "message": "cursor must be an offset"}
        ) from exc
    if offset < 0:
        raise HTTPException(
            400, detail={"code": "BAD_CURSOR", "message": "cursor must not be negative"}
        )
    return offset


@router.get("/companies")
def list_companies(
    q: str | None = None,
    sort: str = "total_postings_seen",
    dir: str = "desc",
    limit: int = 50,
    cursor: str | None = None,
    user: AuthedUser = Depends(require_admin),
):
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = _offset_from(cursor)
    column = _SORTABLE.get(sort, "total_postings_seen")
    direction = "ASC" if dir == "asc" else "DESC"
    # Sorting is applied to the whole aggregate before the page is cut, not
    # within the slice - a page-local sort would reorder 50 rows and call it a
    # ranking of 7,564.
    order = f"{column} {direction} NULLS LAST"
    pattern = f"%{q.strip().lower()}%" if q and q.strip() else None
    params = {"q": pattern, "limit": limit + 1, "offset": offset}

    rows = db.query(_BASE_SQL.format(order=order), params)
    has_more = len(rows) > limit
    rows = rows[:limit]
    keys = [r["company_key"] for r in rows]

    currency = _bucket(db.query(_CURRENCY_SQL, {"keys": keys})) if keys else {}
    statuses = _bucket(db.query(_STATUS_SQL, {"keys": keys})) if keys else {}
    open_rows = {r["company_key"]: r for r in db.query(_OPEN_SQL, {"keys": keys})} if keys else {}
    reposts = (
        {
            r["company_key"]: r
            for r in db.query(
                _REPOST_SQL,
                {
                    "keys": keys,
                    "min_urls": signals.REPOST_MIN_URLS,
                    "min_span": signals.REPOST_MIN_SPAN_DAYS,
                },
            )
        }
        if keys
        else {}
    )

    total_row = db.query_one(_COUNT_SQL, {"q": pattern})
    return {
        "items": [
            _item(
                row,
                currency=currency.get(row["company_key"], []),
                statuses=statuses.get(row["company_key"], []),
                open_row=open_rows.get(row["company_key"]),
                repost=reposts.get(row["company_key"]),
            )
            for row in rows
        ],
        "has_more": has_more,
        "next_cursor": str(offset + limit) if has_more else None,
        "total_names": int(total_row["c"]) if total_row else 0,
        "caveats": _CAVEATS,
    }
