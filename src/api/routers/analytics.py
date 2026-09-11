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

import datetime
import decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db, signals
from api.auth import AuthedUser
from api.rates import DEFAULT_MIN_SAMPLE, Rate
from api.rates import rate as _rate
from api.routers.admin import require_admin
from core.checks import POSTING_CHECK_NAMES

router = APIRouter(prefix="/analytics")


# The checks whose latest verdict is a per-job yes/no, which is the registry:
# 'content' and 'extraction' are not in it, deliberately, because they record
# a scrape attempt rather than a judgement about the posting and neither
# writes a 'rejected' row.
_VERDICT_CHECKS = POSTING_CHECK_NAMES


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
# ALL TIME, deliberately, and now said out loud in the response.
#
# /admin/spend defaults to 30 days and this takes no window at all, so a reader
# comparing the two was comparing a lifetime against a month with nothing on
# either page saying so. Found by personal-portfolio-e3 while checking whether
# the two spend figures could be reconciled - they cannot, and this was the
# difference nobody could see.
#
# All-time is the right window HERE: the question is whether a board has earned
# its keep, which is a question about its whole life, not the last month.
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


# Every query above aggregates by board, so every row one returns is keyed by
# one. The shapes below are those rows: a column the shape does not declare
# raises where it is read rather than surfacing as a missing field in a chart.
class _SourceKeyed(BaseModel):
    source: str


class _InventoryRow(_SourceKeyed):
    total: int
    active: int
    inactive: int
    with_date_posted: int
    posted_7d: int
    posted_30d: int
    median_age_days: float | None
    p90_age_days: float | None
    first_loaded_at: datetime.datetime
    last_loaded_at: datetime.datetime


class _FunnelRow(_SourceKeyed):
    check_type: str
    checked: int
    passed: int
    rejected: int


class _CustomRow(_SourceKeyed):
    jobs_evaluated: int
    jobs_passed_all: int
    evaluations: int
    evaluations_passed: int


class _FirstClosedRow(_SourceKeyed):
    first_checked: int
    dead_on_arrival: int


class _SpendRow(_SourceKeyed):
    model: str | None
    calls: int
    priced_calls: int
    total_tokens: int
    cost_usd: decimal.Decimal | None


class _IngestRow(_SourceKeyed):
    runs: int
    succeeded: int
    failed: int
    last_success_at: datetime.datetime | None
    last_failure_at: datetime.datetime | None


class _YieldRow(_SourceKeyed):
    board_rows: int
    with_status: int
    applied: int
    users: int


class _OverlapRow(_SourceKeyed):
    keyed_jobs: int
    shared_with_other_source: int


class _ConfigRow(_SourceKeyed):
    listings_url: str
    active: bool


class BoardInventory(BaseModel):
    """How many postings this board has supplied, and how many it still calls
    live.

    `active_share` is comparable only between boards with the same
    `reports_inactive`. False means nothing this board ever supplied is marked
    inactive, which means its feed has no way to say otherwise - a fact about
    the feed's format, not about the board.
    """

    total: int
    active: int
    inactive: int
    active_share: Rate
    reports_inactive: bool


class BoardFreshness(BaseModel):
    """Age comes from `date_posted`, never from `created_at`: created_at is
    when a row was loaded into this catalog and the whole table shares a floor
    from the last reseed. `with_date_posted` therefore reports how much of the
    board these ages actually cover.
    """

    with_date_posted: Rate
    median_age_days: float | None
    p90_age_days: float | None
    posted_7d: int
    posted_30d: int
    first_loaded_at: datetime.datetime | None
    last_loaded_at: datetime.datetime | None


class BoardIngest(BaseModel):
    """`last_success_at` is when the pull last ran cleanly.
    `freshness.last_loaded_at` is when it last produced a posting this catalog
    had not seen. The gap between the two is a board serving a frozen feed.
    """

    runs: int
    succeeded: int
    failed: int
    last_success_at: datetime.datetime | None
    last_failure_at: datetime.datetime | None
    success_rate: Rate


class CheckStage(BaseModel):
    """One check's latest verdict per posting, counted.

    `coverage` is how much of the board the stage has seen at all: a pass rate
    over 2% of a board says almost nothing about the other 98%.
    """

    checked: int
    passed: int
    rejected: int
    pass_rate: Rate
    coverage: Rate


class CustomCheckStage(CheckStage):
    """Custom filters are per user and one posting can be judged by several,
    so there are two honest denominators and both are here. `checked` and
    `pass_rate` are job-level - a posting passed every filter it was put in
    front of - and `evaluations` counts the judgements behind them.
    """

    evaluations: int
    evaluations_passed: int


class BoardDecay(BaseModel):
    """`dead_on_arrival` is the FIRST closed-check on a posting already saying
    closed: what the board handed over. `still_open` is the latest verdict:
    what has died since.
    """

    dead_on_arrival: Rate
    still_open: Rate


class ModelSpend(BaseModel):
    """`model` is null for the scrape-only checks, which record an attempt
    rather than a judgement and carry neither a model nor a cost."""

    model: str | None
    calls: int
    total_tokens: int
    cost_usd: float | None


class BoardSpend(BaseModel):
    """What this board's postings cost to check, as billed at the time.

    `window` is in the response because /admin/spend defaults to 30 days and
    this takes no window at all, so a reader comparing the two was comparing a
    lifetime against a month with nothing saying so. All-time is right here:
    whether a board earned its keep is a question about its whole life.

    Not split per check_type: verify_new books one batched call against the
    `closed` row and writes the clearance verdict from the same response, so a
    split would read as though clearance were free on some paths.
    `priced_coverage` keeps "this cost nothing" apart from "we do not know what
    this cost".
    """

    window: str
    calls: int
    total_tokens: int
    cost_usd: float
    priced_coverage: Rate
    by_model: list[ModelSpend]


class BoardOverlap(BaseModel):
    """Keyed on (company, title), because jobs.url is unique table-wide: a
    posting belongs to exactly one source by construction, so duplication is
    invisible at the url level."""

    keyed_jobs: int
    shared_with_other_source: int
    duplicated_share: Rate
    exclusive: int


class BoardYield(BaseModel):
    """What the board's postings became once a person saw them."""

    board_rows: int
    with_status: int
    applied: int
    users: int
    apply_rate: Rate


class BoardDrill(BaseModel):
    """Where the frontend goes to see the rows behind a number, built on the
    server so the filter that produced an aggregate and the filter that
    fetches its members cannot drift apart.

    `checked_jobs` is deliberately the funnel denominator and not the posting
    count: /v1/admin/jobs aggregates ai_queries and only knows postings that
    have a verdict.
    """

    checked_jobs: str
    queries: str
    closed_rejected: str
    clearance_rejected: str
    custom_rejected: str


class BoardAnalytics(BaseModel):
    """One board.

    `configured` is false for a source that has supplied postings and has no
    catalog row. `funnel` is keyed by check name rather than declaring one
    field per check, because the checks are a registry (core.checks) and a
    model enumerating them would be the literal that registry exists to
    remove; the `custom` key carries the wider shape.
    """

    source: str
    configured: bool
    listings_url: str | None
    source_active: bool | None
    inventory: BoardInventory
    freshness: BoardFreshness
    ingest: BoardIngest
    funnel: dict[str, CustomCheckStage | CheckStage]
    decay: BoardDecay
    spend: BoardSpend
    overlap: BoardOverlap
    board_yield: BoardYield
    drill: BoardDrill


class BoardAnalyticsList(BaseModel):
    """`caveats` ships with the numbers rather than beside them on a wiki,
    because a caveat a reader has to go and find is one that will be missed."""

    rows: list[BoardAnalytics]
    min_sample: int
    caveats: list[str]


class OverlapPartner(_SourceKeyed):
    """Another board carrying the same (company, title) as this one."""

    shared_postings: int


class BoardAnalyticsDetail(BaseModel):
    row: BoardAnalytics
    overlap_partners: list[OverlapPartner]
    min_sample: int
    caveats: list[str]


def _by_source[R: _SourceKeyed](rows: list[R]) -> dict[str, R]:
    return {r.source: r for r in rows if r.source}


def _drill(source: str) -> BoardDrill:
    sources = urlencode({"sources": source})
    queries = f"/v1/admin/queries?{sources}"
    return BoardDrill(
        # Deliberately NOT a link to the postings count. This drills the rows
        # behind the FUNNEL denominators - urls this board has had checked -
        # because /v1/admin/jobs aggregates ai_queries and only knows postings
        # that have a verdict. Nothing serves "every posting from this source"
        # to an admin, and pointing inventory.total at an endpoint counting a
        # subset is the drift these links exist to prevent.
        checked_jobs=f"/v1/admin/jobs?{sources}",
        queries=queries,
        closed_rejected=f"{queries}&check_type=closed&status=rejected",
        clearance_rejected=f"{queries}&check_type=clearance&status=rejected",
        custom_rejected=f"{queries}&check_type=custom&status=rejected",
    )


def _funnel_stage(row: _FunnelRow | None, total: int, min_sample: int) -> CheckStage:
    checked = row.checked if row else 0
    passed = row.passed if row else 0
    return CheckStage(
        checked=checked,
        passed=passed,
        rejected=checked - passed,
        pass_rate=_rate(passed, checked, min_sample),
        # How much of the board the stage has actually seen. A pass rate over
        # 2% of a board says almost nothing about the other 98%.
        coverage=_rate(checked, total, min_sample),
    )


def _source_row(
    source: str,
    *,
    min_sample: int,
    config: _ConfigRow | None,
    inventory: _InventoryRow | None,
    funnel: dict[str, _FunnelRow],
    custom: _CustomRow | None,
    first_closed: _FirstClosedRow | None,
    spend: list[_SpendRow],
    ingest: _IngestRow | None,
    board: _YieldRow | None,
    overlap: _OverlapRow | None,
) -> BoardAnalytics:
    total = inventory.total if inventory else 0
    active = inventory.active if inventory else 0
    inactive = inventory.inactive if inventory else 0

    stages: dict[str, CustomCheckStage | CheckStage] = {
        c: _funnel_stage(funnel.get(c), total, min_sample) for c in _VERDICT_CHECKS
    }
    custom_evaluated = custom.jobs_evaluated if custom else 0
    custom_passed = custom.jobs_passed_all if custom else 0
    stages["custom"] = CustomCheckStage(
        checked=custom_evaluated,
        passed=custom_passed,
        rejected=custom_evaluated - custom_passed,
        pass_rate=_rate(custom_passed, custom_evaluated, min_sample),
        coverage=_rate(custom_evaluated, total, min_sample),
        # One job judged by three filters is three evaluations but one job;
        # both denominators are real and they answer different questions.
        evaluations=custom.evaluations if custom else 0,
        evaluations_passed=custom.evaluations_passed if custom else 0,
    )

    first_checked = first_closed.first_checked if first_closed else 0
    doa = first_closed.dead_on_arrival if first_closed else 0

    keyed = overlap.keyed_jobs if overlap else 0
    shared = overlap.shared_with_other_source if overlap else 0

    board_rows = board.board_rows if board else 0
    applied = board.applied if board else 0

    return BoardAnalytics(
        source=source,
        configured=config is not None,
        listings_url=config.listings_url if config else None,
        source_active=config.active if config else None,
        inventory=BoardInventory(
            total=total,
            active=active,
            inactive=inactive,
            active_share=_rate(active, total, min_sample),
            # False means every row this board has ever supplied is still
            # marked active, which means the feed has no way to say otherwise -
            # so active_share is a fact about the feed, not about the board.
            reports_inactive=inactive > 0,
        ),
        freshness=BoardFreshness(
            with_date_posted=_rate(
                inventory.with_date_posted if inventory else 0, total, min_sample
            ),
            median_age_days=_round_days(inventory.median_age_days if inventory else None),
            p90_age_days=_round_days(inventory.p90_age_days if inventory else None),
            posted_7d=inventory.posted_7d if inventory else 0,
            posted_30d=inventory.posted_30d if inventory else 0,
            first_loaded_at=inventory.first_loaded_at if inventory else None,
            last_loaded_at=inventory.last_loaded_at if inventory else None,
        ),
        ingest=BoardIngest(
            runs=ingest.runs if ingest else 0,
            succeeded=ingest.succeeded if ingest else 0,
            failed=ingest.failed if ingest else 0,
            last_success_at=ingest.last_success_at if ingest else None,
            last_failure_at=ingest.last_failure_at if ingest else None,
            success_rate=_rate(
                ingest.succeeded if ingest else 0,
                ingest.runs if ingest else 0,
                min_sample,
            ),
        ),
        funnel=stages,
        decay=BoardDecay(
            dead_on_arrival=_rate(doa, first_checked, min_sample),
            still_open=stages["closed"].pass_rate,
        ),
        spend=_spend_summary(spend, min_sample),
        overlap=BoardOverlap(
            keyed_jobs=keyed,
            shared_with_other_source=shared,
            duplicated_share=_rate(shared, keyed, min_sample),
            exclusive=keyed - shared,
        ),
        board_yield=BoardYield(
            board_rows=board_rows,
            with_status=board.with_status if board else 0,
            applied=applied,
            users=board.users if board else 0,
            apply_rate=_rate(applied, board_rows, min_sample),
        ),
        drill=_drill(source),
    )


def _round_days(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _spend_summary(rows: list[_SpendRow], min_sample: int) -> BoardSpend:
    """What this board's postings cost to check, as billed at the time.

    Deliberately not broken down per check_type: verify_new books one batched
    call against the `closed` row and writes the clearance verdict from the
    same response, so a per-check split reads as though clearance were free on
    some paths and not others. The per-board total is unambiguous; the split
    is not.
    """
    calls = sum(r.calls for r in rows)
    priced_calls = sum(r.priced_calls for r in rows)
    cost = sum((r.cost_usd for r in rows if r.cost_usd is not None), decimal.Decimal(0))
    return BoardSpend(
        # Said out loud, because /admin/spend defaults to 30 days and this does
        # not. A reader comparing the two was comparing a lifetime against a
        # month with nothing on either page saying so.
        window="all_time",
        calls=calls,
        total_tokens=sum(r.total_tokens for r in rows),
        cost_usd=float(cost),
        # Rows with no price are the scrape-only checks (content, extraction)
        # and any model missing from the price table. Reporting coverage keeps
        # "cheap" distinguishable from "partly unpriced".
        priced_coverage=_rate(priced_calls, calls, min_sample),
        by_model=[
            ModelSpend(
                model=r.model,
                calls=r.calls,
                total_tokens=r.total_tokens,
                cost_usd=float(r.cost_usd) if r.cost_usd is not None else None,
            )
            for r in sorted(rows, key=lambda r: -r.calls)
        ],
    )


def _collect(min_sample: int) -> list[BoardAnalytics]:
    configs = _by_source(
        db.query_as(_ConfigRow, "SELECT name AS source, listings_url, active FROM sources")
    )
    inventory = _by_source(db.query_as(_InventoryRow, _INVENTORY_SQL))
    custom = _by_source(db.query_as(_CustomRow, _CUSTOM_SQL))
    first_closed = _by_source(db.query_as(_FirstClosedRow, _FIRST_CLOSED_SQL))
    ingest = _by_source(db.query_as(_IngestRow, _INGEST_SQL))
    board = _by_source(db.query_as(_YieldRow, _YIELD_SQL))
    overlap = _by_source(db.query_as(_OverlapRow, _OVERLAP_SQL))

    funnel: dict[str, dict[str, _FunnelRow]] = {}
    for row in db.query_as(_FunnelRow, _FUNNEL_SQL, {"checks": list(_VERDICT_CHECKS)}):
        funnel.setdefault(row.source, {})[row.check_type] = row

    spend: dict[str, list[_SpendRow]] = {}
    for row in db.query_as(_SpendRow, _SPEND_SQL):
        spend.setdefault(row.source, []).append(row)

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
) -> BoardAnalyticsList:
    return BoardAnalyticsList(
        rows=_collect(min_sample),
        min_sample=min_sample,
        caveats=_CAVEATS,
    )


@router.get("/sources/{source}")
def source_detail(
    source: str,
    min_sample: int = Query(DEFAULT_MIN_SAMPLE, ge=1, le=10_000),
    user: AuthedUser = Depends(require_admin),
) -> BoardAnalyticsDetail:
    row = next((r for r in _collect(min_sample) if r.source == source), None)
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    return BoardAnalyticsDetail(
        row=row,
        overlap_partners=db.query_as(OverlapPartner, _OVERLAP_PARTNERS_SQL, {"source": source}),
        min_sample=min_sample,
        caveats=_CAVEATS,
    )
