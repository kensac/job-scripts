"""Typed source analytics query rows and API responses."""

import datetime
import decimal

from pydantic import BaseModel

from api.rates import Rate


# Every aggregate row is keyed by one source. Undeclared columns fail where
# read rather than surfacing as missing fields in a chart.
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


class _PopulationsRow(_SourceKeyed):
    acted_on: int
    working_set: int
    visible: int


class _OverlapRow(_SourceKeyed):
    keyed_jobs: int
    shared_with_other_source: int


class _ConfigRow(_SourceKeyed):
    listings_url: str
    active: bool


class BoardInventory(BaseModel):
    """How many postings this board has supplied, and how many it still calls
    live.

    `active` is how many are available (catalog.IS_AVAILABLE). An
    aggregator's absence never closes a posting, so compare `active_share`
    between company boards, or between aggregators.
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
    """`model` is null only for a paid call whose model was never recorded."""

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
    this cost". `calls` are paid provider requests: a page fetch is not one.
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

    with_status: int
    applied: int
    users: int
    apply_rate: Rate
    acted_on: int
    working_set: int
    visible: int


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
