"""Per-board analytics: what each source supplies, and what it is worth.

Every rate here ships with the numerator and denominator that produced it, and
returns a null value rather than a number when the denominator is too small to
mean anything. Boards in this catalog span 18,876 postings down to 1, so an
un-floored rate would let the smallest board render the loudest percentage.

Two shape caveats are baked into the response rather than left for the reader
to rediscover, because both invert the obvious reading of the numbers:

`jobs.active` is not comparable across boards. catalog.upsert_postings sets it
straight from the feed (`active = EXCLUDED.active`) and nothing else in the
codebase ever clears it, so a board whose feed lists only live postings keeps
every row it has ever seen at active=true, while a board whose feed carries an
explicit per-posting flag accumulates rows marked inactive. That is a
difference in feed format, not in board behaviour. `reports_inactive` says
which kind each board is, and the closed-check funnel below is the instrument
that IS applied uniformly to every board.

`jobs.created_at` is when a row was loaded into this catalog, not when the
posting was discovered - the whole table shares a floor from the last reseed,
while ai_queries reaches further back. Freshness therefore comes from
date_posted, and `with_date_posted` reports how much of the board that covers.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query

from api import db, signals
from api.auth import AuthedUser
from api.routers.admin import require_admin

router = APIRouter(prefix="/analytics")

# A proportion needs enough trials before it carries information. Thirty is the
# conventional floor for the normal approximation to the binomial: below it the
# Wald interval stops covering, and a single extra rejection moves the rate by
# whole percentage points. It is a policy rather than a constant of nature, so
# callers can raise or lower it per request.
DEFAULT_MIN_SAMPLE = 30

# The checks whose latest verdict is a per-job yes/no. 'content' and
# 'extraction' are excluded deliberately: they record a scrape attempt, not a
# judgement about the posting, and neither writes a 'rejected' row.
_VERDICT_CHECKS = ("closed", "clearance")


def _rate(numerator: int, denominator: int, min_sample: int) -> dict[str, Any]:
    """A proportion that refuses to be a bare number.

    Below the floor `value` is None and the caller renders "2 of 7"; the
    numerator and denominator are always present so it can.
    """
    below = denominator < min_sample
    return {
        "value": None if below or not denominator else round(numerator / denominator, 4),
        "numerator": numerator,
        "denominator": denominator,
        "below_floor": below,
    }


_INVENTORY_SQL = """
SELECT source,
       count(*) AS total,
       count(*) FILTER (WHERE active) AS active,
       count(*) FILTER (WHERE NOT active) AS inactive,
       count(date_posted) AS with_date_posted,
       count(*) FILTER (WHERE date_posted >= now() - interval '7 days') AS posted_7d,
       count(*) FILTER (WHERE date_posted >= now() - interval '30 days') AS posted_30d,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY extract(epoch FROM now() - date_posted) / 86400.0
       ) AS median_age_days,
       percentile_cont(0.9) WITHIN GROUP (
           ORDER BY extract(epoch FROM now() - date_posted) / 86400.0
       ) AS p90_age_days,
       min(created_at) AS first_loaded_at,
       max(created_at) AS last_loaded_at
FROM jobs
GROUP BY source
"""

# Joining jobs before deduping is load-bearing for speed, not just tidiness:
# DISTINCT ON over (url, check_type, id) sorts a 46k-row set keyed by a long
# text url and spills to disk (~1.15s measured on prod). Carrying jobs.id
# through and sorting on (job_id, check_type, id) keeps the same sort in
# memory and costs ~245ms for identical output.
_FUNNEL_SQL = """
WITH q AS (
    SELECT j.source AS source, j.id AS job_id, a.check_type, a.status, a.id AS qid
    FROM ai_queries a
    JOIN jobs j ON j.url = a.url
    WHERE a.check_type = ANY(%(checks)s) AND a.status IN ('passed', 'rejected')
), latest AS (
    SELECT DISTINCT ON (job_id, check_type) source, check_type, status
    FROM q ORDER BY job_id, check_type, qid DESC
)
SELECT source, check_type,
       count(*) AS checked,
       count(*) FILTER (WHERE status = 'passed') AS passed,
       count(*) FILTER (WHERE status = 'rejected') AS rejected
FROM latest GROUP BY source, check_type
"""

# Custom filters are per-user and a job can be judged by several of them, so
# there are two honest denominators and this returns both: evaluations (one per
# job per filter prompt) and jobs that passed every filter they were put in
# front of, which is the job-level predicate the board itself applies.
_CUSTOM_SQL = """
WITH q AS (
    SELECT j.source AS source, j.id AS job_id, a.prompt_hash, a.status, a.id AS qid
    FROM ai_queries a
    JOIN jobs j ON j.url = a.url
    WHERE a.check_type = 'custom' AND a.status IN ('passed', 'rejected')
      AND a.prompt_hash IS NOT NULL
), latest AS (
    SELECT DISTINCT ON (job_id, prompt_hash) source, job_id, status
    FROM q ORDER BY job_id, prompt_hash, qid DESC
), per_job AS (
    SELECT source, job_id,
           count(*) AS evaluations,
           count(*) FILTER (WHERE status = 'passed') AS passed
    FROM latest GROUP BY source, job_id
)
SELECT source,
       count(*) AS jobs_evaluated,
       count(*) FILTER (WHERE passed = evaluations) AS jobs_passed_all,
       sum(evaluations) AS evaluations,
       sum(passed) AS evaluations_passed
FROM per_job GROUP BY source
"""

# Dead on arrival is defined once, in api.signals, because the per-job
# intelligence pane serves the same number for a single board. Two copies of a
# "first verdict per posting" query is how the two readings would drift.
_FIRST_CLOSED_SQL = signals.first_closed_sql()

# cost_usd is summed, never recomputed. It is written at call time against the
# price that was in force then, so re-deriving it on read would silently
# restate history every time the price table changes.
#
# Grouped by model because a board's bill is only interpretable next to what
# was run on it, and `priced` separates "this cost nothing" from "we do not
# know what this cost": content and extraction rows carry no model and no
# cost, and folding them into a zero would understate the bill.
_SPEND_SQL = """
SELECT j.source AS source, a.model,
       count(*) AS calls,
       count(a.cost_usd) AS priced_calls,
       sum(coalesce(a.total_tokens, 0)) AS total_tokens,
       sum(a.cost_usd) AS cost_usd
FROM ai_queries a
JOIN jobs j ON j.url = a.url
GROUP BY j.source, a.model
"""

# last_success_at is when the ingest last ran cleanly; jobs.last_loaded_at (from
# _INVENTORY_SQL) is when it last produced a posting this catalog had not seen.
# They diverge, and the gap is the signal: a board whose ingest has succeeded
# hourly for a week without yielding a new row is serving a frozen feed.
_INGEST_SQL = """
SELECT payload->>'source' AS source,
       count(*) AS runs,
       count(*) FILTER (WHERE status = 'done') AS succeeded,
       count(*) FILTER (WHERE status = 'failed') AS failed,
       max(finished_at) FILTER (WHERE status = 'done') AS last_success_at,
       max(finished_at) FILTER (WHERE status = 'failed') AS last_failure_at
FROM tasks
WHERE kind = 'ingest_source' AND payload->>'source' IS NOT NULL
GROUP BY payload->>'source'
"""

_YIELD_SQL = """
SELECT j.source AS source,
       count(*) AS board_rows,
       count(*) FILTER (WHERE uj.status IS NOT NULL AND uj.status <> '') AS with_status,
       count(*) FILTER (WHERE uj.date_applied IS NOT NULL) AS applied,
       count(DISTINCT uj.user_id) AS users
FROM user_jobs uj
JOIN jobs j ON j.id = uj.job_id
WHERE NOT uj.hidden
GROUP BY j.source
"""

# Overlap keys on (company, title) because a url cannot express it: jobs.url is
# unique table-wide, so a posting belongs to exactly one source by
# construction and cross-source duplication is invisible at the url level.
#
# min(source) <> max(source) over the partition is "more than one distinct
# source carries this posting" - count(DISTINCT) is not a window function, and
# computing the distinct count as a separate aggregate means hashing the pairs
# and then joining them back, which measured 477ms against prod versus 358ms
# for the single windowed pass.
_OVERLAP_SQL = """
SELECT source,
       count(*) AS keyed_jobs,
       count(*) FILTER (WHERE lo <> hi) AS shared_with_other_source
FROM (
    SELECT source, min(source) OVER w AS lo, max(source) OVER w AS hi
    FROM (
        SELECT source, lower(btrim(company)) AS company, lower(btrim(title)) AS title
        FROM jobs WHERE company <> '' AND title <> ''
    ) keyed
    WINDOW w AS (PARTITION BY company, title)
) spread
GROUP BY source
"""

_OVERLAP_PARTNERS_SQL = """
WITH keyed AS (
    SELECT source, lower(btrim(company)) AS company, lower(btrim(title)) AS title
    FROM jobs WHERE company <> '' AND title <> ''
), mine AS (
    SELECT company, title FROM keyed WHERE source = %(source)s
)
SELECT k.source AS source, count(*) AS shared_postings
FROM keyed k JOIN mine m ON m.company = k.company AND m.title = k.title
WHERE k.source <> %(source)s
GROUP BY k.source ORDER BY shared_postings DESC
"""


def _by_source(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["source"]: r for r in rows if r.get("source")}


def _drill(source: str) -> dict[str, str]:
    """Where the frontend goes to see the rows behind a number.

    Built here rather than in the client so the filter that produced an
    aggregate and the filter that fetches its members cannot drift apart.
    """
    sources = urlencode({"sources": source})
    queries = f"/v1/admin/queries?{sources}"
    return {
        # Deliberately NOT a link to the postings count. This drills the rows
        # behind the FUNNEL denominators - urls this board has had checked -
        # because /v1/admin/jobs aggregates ai_queries and only knows postings
        # that have a verdict. Nothing serves "every posting from this source"
        # to an admin, and pointing inventory.total at an endpoint counting a
        # subset is the drift these links exist to prevent.
        "checked_jobs": f"/v1/admin/jobs?{sources}",
        "queries": queries,
        "closed_rejected": f"{queries}&check_type=closed&status=rejected",
        "clearance_rejected": f"{queries}&check_type=clearance&status=rejected",
        "custom_rejected": f"{queries}&check_type=custom&status=rejected",
    }


def _funnel_stage(row: dict[str, Any] | None, total: int, min_sample: int) -> dict[str, Any]:
    checked = int(row["checked"]) if row else 0
    passed = int(row["passed"]) if row else 0
    return {
        "checked": checked,
        "passed": passed,
        "rejected": checked - passed,
        "pass_rate": _rate(passed, checked, min_sample),
        # How much of the board the stage has actually seen. A pass rate over
        # 2% of a board says almost nothing about the other 98%.
        "coverage": _rate(checked, total, min_sample),
    }


def _source_row(
    source: str,
    *,
    min_sample: int,
    config: dict[str, Any] | None,
    inventory: dict[str, Any] | None,
    funnel: dict[str, dict[str, Any]],
    custom: dict[str, Any] | None,
    first_closed: dict[str, Any] | None,
    spend: list[dict[str, Any]],
    ingest: dict[str, Any] | None,
    board: dict[str, Any] | None,
    overlap: dict[str, Any] | None,
) -> dict[str, Any]:
    total = int(inventory["total"]) if inventory else 0
    active = int(inventory["active"]) if inventory else 0
    inactive = int(inventory["inactive"]) if inventory else 0

    stages = {c: _funnel_stage(funnel.get(c), total, min_sample) for c in _VERDICT_CHECKS}
    custom_evaluated = int(custom["jobs_evaluated"]) if custom else 0
    custom_passed = int(custom["jobs_passed_all"]) if custom else 0
    stages["custom"] = {
        "checked": custom_evaluated,
        "passed": custom_passed,
        "rejected": custom_evaluated - custom_passed,
        "pass_rate": _rate(custom_passed, custom_evaluated, min_sample),
        "coverage": _rate(custom_evaluated, total, min_sample),
        # One job judged by three filters is three evaluations but one job;
        # both denominators are real and they answer different questions.
        "evaluations": int(custom["evaluations"]) if custom else 0,
        "evaluations_passed": int(custom["evaluations_passed"]) if custom else 0,
    }

    first_checked = int(first_closed["first_checked"]) if first_closed else 0
    doa = int(first_closed["dead_on_arrival"]) if first_closed else 0

    keyed = int(overlap["keyed_jobs"]) if overlap else 0
    shared = int(overlap["shared_with_other_source"]) if overlap else 0

    board_rows = int(board["board_rows"]) if board else 0
    applied = int(board["applied"]) if board else 0

    return {
        "source": source,
        "configured": config is not None,
        "listings_url": config["listings_url"] if config else None,
        "source_active": config["active"] if config else None,
        "inventory": {
            "total": total,
            "active": active,
            "inactive": inactive,
            "active_share": _rate(active, total, min_sample),
            # False means every row this board has ever supplied is still
            # marked active, which means the feed has no way to say otherwise -
            # so active_share is a fact about the feed, not about the board.
            "reports_inactive": inactive > 0,
        },
        "freshness": {
            "with_date_posted": _rate(
                int(inventory["with_date_posted"]) if inventory else 0, total, min_sample
            ),
            "median_age_days": _round_days(inventory, "median_age_days"),
            "p90_age_days": _round_days(inventory, "p90_age_days"),
            "posted_7d": int(inventory["posted_7d"]) if inventory else 0,
            "posted_30d": int(inventory["posted_30d"]) if inventory else 0,
            "first_loaded_at": inventory["first_loaded_at"] if inventory else None,
            "last_loaded_at": inventory["last_loaded_at"] if inventory else None,
        },
        "ingest": {
            "runs": int(ingest["runs"]) if ingest else 0,
            "succeeded": int(ingest["succeeded"]) if ingest else 0,
            "failed": int(ingest["failed"]) if ingest else 0,
            "last_success_at": ingest["last_success_at"] if ingest else None,
            "last_failure_at": ingest["last_failure_at"] if ingest else None,
            "success_rate": _rate(
                int(ingest["succeeded"]) if ingest else 0,
                int(ingest["runs"]) if ingest else 0,
                min_sample,
            ),
        },
        "funnel": stages,
        "decay": {
            "dead_on_arrival": _rate(doa, first_checked, min_sample),
            "still_open": stages["closed"]["pass_rate"],
        },
        "spend": _spend_summary(spend, min_sample),
        "overlap": {
            "keyed_jobs": keyed,
            "shared_with_other_source": shared,
            "duplicated_share": _rate(shared, keyed, min_sample),
            "exclusive": keyed - shared,
        },
        "board_yield": {
            "board_rows": board_rows,
            "with_status": int(board["with_status"]) if board else 0,
            "applied": applied,
            "users": int(board["users"]) if board else 0,
            "apply_rate": _rate(applied, board_rows, min_sample),
        },
        "drill": _drill(source),
    }


def _round_days(inventory: dict[str, Any] | None, key: str) -> float | None:
    value = inventory.get(key) if inventory else None
    return round(float(value), 1) if value is not None else None


def _spend_summary(rows: list[dict[str, Any]], min_sample: int) -> dict[str, Any]:
    """What this board's postings cost to check, as billed at the time.

    Deliberately not broken down per check_type: verify_new books one batched
    call against the `closed` row and writes the clearance verdict from the
    same response, so a per-check split reads as though clearance were free on
    some paths and not others. The per-board total is unambiguous; the split
    is not.
    """
    calls = sum(int(r["calls"]) for r in rows)
    priced_calls = sum(int(r["priced_calls"]) for r in rows)
    cost = sum(r["cost_usd"] for r in rows if r["cost_usd"] is not None)
    return {
        "calls": calls,
        "total_tokens": sum(int(r["total_tokens"]) for r in rows),
        "cost_usd": float(cost),
        # Rows with no price are the scrape-only checks (content, extraction)
        # and any model missing from the price table. Reporting coverage keeps
        # "cheap" distinguishable from "partly unpriced".
        "priced_coverage": _rate(priced_calls, calls, min_sample),
        "by_model": [
            {
                "model": r["model"],
                "calls": int(r["calls"]),
                "total_tokens": int(r["total_tokens"]),
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
            }
            for r in sorted(rows, key=lambda r: -int(r["calls"]))
        ],
    }


def _collect(min_sample: int) -> list[dict[str, Any]]:
    configs = _by_source(db.query("SELECT name AS source, listings_url, active FROM sources"))
    inventory = _by_source(db.query(_INVENTORY_SQL))
    custom = _by_source(db.query(_CUSTOM_SQL))
    first_closed = _by_source(db.query(_FIRST_CLOSED_SQL))
    ingest = _by_source(db.query(_INGEST_SQL))
    board = _by_source(db.query(_YIELD_SQL))
    overlap = _by_source(db.query(_OVERLAP_SQL))

    funnel: dict[str, dict[str, dict[str, Any]]] = {}
    for row in db.query(_FUNNEL_SQL, {"checks": list(_VERDICT_CHECKS)}):
        funnel.setdefault(row["source"], {})[row["check_type"]] = row

    spend: dict[str, list[dict[str, Any]]] = {}
    for row in db.query(_SPEND_SQL):
        spend.setdefault(row["source"], []).append(row)

    # A configured source with no postings is not an empty row to skip - it is
    # a board that is being ingested and returning nothing, which is the
    # loudest thing this endpoint can say about it.
    names = set(configs) | set(inventory) | set(ingest)
    return [
        _source_row(
            name,
            min_sample=min_sample,
            config=configs.get(name),
            inventory=inventory.get(name),
            funnel=funnel.get(name, {}),
            custom=custom.get(name),
            first_closed=first_closed.get(name),
            spend=spend.get(name, []),
            ingest=ingest.get(name),
            board=board.get(name),
            overlap=overlap.get(name),
        )
        for name in sorted(names)
    ]


_CAVEATS = [
    "jobs.active reflects only what a board's feed last reported; nothing in "
    "the ingest path ever clears it. Compare active_share only between boards "
    "with the same reports_inactive value.",
    "jobs.created_at is when a posting was loaded into this catalog, not when "
    "it was discovered. first_loaded_at is bounded below by the last reseed; "
    "use date_posted for age.",
    "No column records when a posting went inactive, so time-to-inactive and a "
    "decay curve are not computable - decay here is the closed-check verdict.",
    "user_jobs.status carries no interview or offer state, so interview and "
    "offer rates per board cannot be computed from this schema.",
    "Cross-source duplication is keyed on (company, title): jobs.url is unique "
    "table-wide, so the same posting cannot appear under two sources.",
]


@router.get("/sources")
def source_analytics(
    min_sample: int = Query(DEFAULT_MIN_SAMPLE, ge=1, le=10_000),
    user: AuthedUser = Depends(require_admin),
):
    rows = _collect(min_sample)
    return {
        "rows": rows,
        "min_sample": min_sample,
        "caveats": _CAVEATS,
    }


@router.get("/sources/{source}")
def source_detail(
    source: str,
    min_sample: int = Query(DEFAULT_MIN_SAMPLE, ge=1, le=10_000),
    user: AuthedUser = Depends(require_admin),
):
    row = next((r for r in _collect(min_sample) if r["source"] == source), None)
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    return {
        "row": row,
        "overlap_partners": db.query(_OVERLAP_PARTNERS_SQL, {"source": source}),
        "min_sample": min_sample,
        "caveats": _CAVEATS,
    }
