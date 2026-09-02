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

from fastapi import APIRouter, Depends, HTTPException, Query

from api import db, mail_pipeline, rates, signals
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
    -- `applications`, not `user_jobs`. The board table only knows postings the
    -- user tracked by hand; `applications` also holds the ones only email
    -- knows about, whose posting was never in this catalog and never will be.
    -- Reading the old table reported 605 companies when 1,283 had evidence,
    -- and it had been that way since the mail pipeline landed - a view that
    -- silently stopped describing the system it names.
    --
    -- Dismissed applications are excluded: a dismissal says the row should
    -- never have existed, so counting it would be counting a known mistake.
    SELECT lower(btrim(a.company_name)) AS company_key,
           count(*) AS applications_n,
           max(a.applied_at)::date AS last_applied_at
    FROM applications a
    WHERE a.company_name IS NOT NULL AND btrim(a.company_name) <> ''
      AND a.dismissed_at IS NULL
    GROUP BY lower(btrim(a.company_name))
)
SELECT base.*,
       COALESCE(apps.applications_n, 0) AS applications_n,
       apps.last_applied_at
FROM base LEFT JOIN apps ON apps.company_key = base.company_key
WHERE (%(q)s::text IS NULL OR base.company_key LIKE %(q)s::text)
ORDER BY {order}, base.company_key
LIMIT %(limit)s OFFSET %(offset)s
"""


# A reply is any classified mail attached to the application. An OUTCOME is a
# reply that decided something - an acknowledgement is a mail server saying it
# received the form, and counting it as "they replied" would report a 100%
# response rate for every company running an ATS.
_OUTCOME_KINDS = (
    "rejection",
    "interview_invite",
    "interview_scheduled",
    "assessment_invite",
    "offer",
    "position_closed",
)

_RESPONSE_SQL = """
WITH scoped AS (
    SELECT a.id, lower(btrim(a.company_name)) AS company_key,
           a.applied_at, a.source_provenance
    FROM applications a
    WHERE a.company_name IS NOT NULL AND btrim(a.company_name) <> ''
      AND a.dismissed_at IS NULL
      AND lower(btrim(a.company_name)) = ANY(%(keys)s)
      -- Applications whose first contact came from a platform rather than an
      -- employer's own mail. A course provider that replies to everyone is not
      -- a company that answers every applicant, and including them put a
      -- perfect response rate on a university.
      -- (No percent sign in this comment on purpose: psycopg reads a bare one
      -- as a placeholder and the query fails to prepare.)
      AND NOT EXISTS (
          SELECT 1 FROM application_matches am2
          JOIN email_messages m2 ON m2.id = am2.message_id
          WHERE am2.application_id = a.id
            AND lower(COALESCE(NULLIF(split_part(m2.from_email, '@', 2), ''), ''))
                = ANY(%(intermediaries)s)
      )
),
latest_event AS (
    SELECT DISTINCT ON (message_id) message_id, kind
    FROM email_events ORDER BY message_id, id DESC
),
per_app AS (
    SELECT s.id, s.company_key, s.applied_at, s.source_provenance,
           count(e.kind) AS replies,
           count(*) FILTER (WHERE e.kind = ANY(%(outcomes)s)) AS outcomes,
           min(m.sent_at) FILTER (WHERE e.kind = ANY(%(outcomes)s)) AS first_outcome_at
    FROM scoped s
    LEFT JOIN application_matches am ON am.application_id = s.id
    LEFT JOIN email_messages m ON m.id = am.message_id
    LEFT JOIN latest_event e ON e.message_id = m.id
    GROUP BY s.id, s.company_key, s.applied_at, s.source_provenance
)
SELECT company_key,
       count(*) AS applications,
       count(*) FILTER (WHERE replies > 0) AS replied,
       count(*) FILTER (WHERE outcomes > 0) AS with_outcome,
       -- Timing is TRACKER-ONLY and the filter is the whole point; see
       -- _response_block. The >= guard drops three rows whose outcome predates
       -- the date entered by hand.
       count(*) FILTER (
           WHERE source_provenance = 'tracker' AND applied_at IS NOT NULL
             AND first_outcome_at IS NOT NULL AND first_outcome_at >= applied_at
       ) AS timed_n,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY CASE
               WHEN source_provenance = 'tracker' AND applied_at IS NOT NULL
                    AND first_outcome_at IS NOT NULL AND first_outcome_at >= applied_at
               THEN EXTRACT(epoch FROM (first_outcome_at - applied_at)) / 86400
           END
       ) AS median_days,
       percentile_cont(0.9) WITHIN GROUP (
           ORDER BY CASE
               WHEN source_provenance = 'tracker' AND applied_at IS NOT NULL
                    AND first_outcome_at IS NOT NULL AND first_outcome_at >= applied_at
               THEN EXTRACT(epoch FROM (first_outcome_at - applied_at)) / 86400
           END
       ) AS p90_days
FROM per_app GROUP BY company_key
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


def _quantile(value: Any, n: int, min_sample: int) -> float | None:
    if value is None or n < min_sample:
        return None
    return round(float(value), 1)


def _response_block(row: dict[str, Any] | None, min_sample: int) -> dict[str, Any] | None:
    """Does this company reply, and how long does it take?

    RATES ARE ALMOST ALWAYS NULL HERE, and that is the honest answer rather than
    a defect. Of 1,283 companies with an application, 449 have two, 87 have
    five and TWO have thirty. At any floor that makes a proportion mean
    something, nearly every company is below it - so the numerator and
    denominator carry the information and the caller renders "2 of 7". A
    company with three applications and one reply does not have a 33% response
    rate; it has one reply.

    TIMING IS TRACKER-ONLY, and this is not a caveat about precision - the
    mail-derived number is not a measurement at all. For all 1,783 mail-derived
    applications, `applied_at` IS the first matched message's `sent_at`,
    exactly, with no exceptions. So "days from applying to the first outcome"
    computed over them measures the gap between the first mail and the first
    DECIDING mail, which is zero whenever a rejection arrives with no
    acknowledgement before it - a median of 0.0 days across 956 applications.
    Publishing that mixed with real dates is how an average becomes 33 days
    when the measurable median is 8.

    Median and p90 rather than a mean, because the tail is long: over the 200
    tracker applications with a clean duration the mean is 30.5 days and the
    median 7.9. The mean is describing the tail, not the wait.
    """
    if row is None:
        return None
    applications = int(row["applications"])
    if not applications:
        return None
    timed = int(row["timed_n"])
    return {
        "replied": rates.rate(int(row["replied"]), applications, min_sample),
        "reached_outcome": rates.rate(int(row["with_outcome"]), applications, min_sample),
        "days_to_first_outcome": {
            "n": timed,
            # A median needs a sample as much as a proportion does. One timed
            # application produced a "median" of 205 days, which is not a
            # median - it is that one application wearing the word.
            "median": _quantile(row["median_days"], timed, min_sample),
            "p90": _quantile(row["p90_days"], timed, min_sample),
            "below_floor": timed < min_sample,
            # Named so nobody reads this as covering every application.
            "basis": "tracker_dated_only",
        },
    }


def _item(
    base: dict[str, Any],
    *,
    currency: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    open_row: dict[str, Any] | None,
    repost: dict[str, Any] | None,
    responses: dict[str, Any] | None = None,
    min_sample: int = rates.DEFAULT_MIN_SAMPLE,
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
        block = _response_block(responses, min_sample)
        if block is not None:
            item["applications"]["responses"] = block
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
    min_sample: int = Query(default=rates.DEFAULT_MIN_SAMPLE, ge=1),
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

    responses = (
        {
            r["company_key"]: r
            for r in db.query(
                _RESPONSE_SQL,
                {
                    "keys": keys,
                    "outcomes": list(_OUTCOME_KINDS),
                    "intermediaries": mail_pipeline.intermediary_domains(),
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
                responses=responses.get(row["company_key"]),
                min_sample=min_sample,
            )
            for row in rows
        ],
        "has_more": has_more,
        "next_cursor": str(offset + limit) if has_more else None,
        # A company that has not replied may simply not have been READ yet.
        # Silence is only evidence once the mailbox is classified, so the size
        # of the backlog travels with the numbers that depend on it rather than
        # being something a reader has to know to ask about.
        "coverage": {
            "messages_awaiting_classification": int(
                (
                    db.query_one(
                        "SELECT count(*) AS c FROM email_messages m WHERE NOT EXISTS "
                        "(SELECT 1 FROM email_events e WHERE e.message_id = m.id)"
                    )
                    or {"c": 0}
                )["c"]
            ),
            "min_sample": min_sample,
        },
        "total_names": int(total_row["c"]) if total_row else 0,
        "caveats": _CAVEATS,
    }
