"""The verdict ledger: every model call ai_queries recorded, by row, by
posting, and in total."""

from __future__ import annotations

import datetime
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import db, pagination, scoping, sorting
from api import params as params_
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin
from core import pricing, reason_taxonomy

router = APIRouter()


_SORTABLE = {
    "id",
    "created_at",
    "check_type",
    "status",
    "company",
    "total_tokens",
    "duration_ms",
}


_LIST_COLS = (
    "id, created_at, config_name, url, check_type, status, reason, model, "
    "company, job_title, prompt_tokens, completion_tokens, total_tokens, "
    "cached_tokens, reasoning_tokens, duration_ms, error, worker, "
    "filter_name, prompt_hash, "
    # Correlated lookups rather than a join: ai_queries and jobs share several
    # column names, so joining would make every existing filter ambiguous.
    "(SELECT j.id FROM jobs j WHERE j.url = ai_queries.url) AS job_id, "
    "(SELECT j.source FROM jobs j WHERE j.url = ai_queries.url) AS source"
)


def _where(
    check_type: str | None,
    status: str | None,
    config: str | None,
    url: str | None,
    q: str | None,
    deep: bool = False,
    sources: str | None = None,
    reason_group: str | None = None,
    evidence_missing: bool = False,
    prompt_hash: str | None = None,
    user_ids: list[int] | None = None,
) -> tuple[str, dict]:
    clauses = []
    params: dict = {}
    if user_ids:
        # A user's rows are the custom verdicts their filters produced; the
        # shared checks belong to nobody and fall out of any user's scope.
        clauses.append(scoping.filters_of())
        params["user_ids"] = user_ids
    wanted_sources = [s.strip() for s in (sources or "").split(",") if s.strip()]
    if wanted_sources:
        # ai_queries is keyed by url; source lives on the job. Subquery instead
        # of a join keeps this composable with the existing count/list queries.
        clauses.append("url IN (SELECT url FROM jobs WHERE source = ANY(%(sources)s))")
        params["sources"] = wanted_sources
    if check_type:
        clauses.append("check_type = %(check_type)s")
        params["check_type"] = check_type
    if status:
        clauses.append("status = %(status)s")
        params["status"] = status
    if config:
        clauses.append("config_name = %(config)s")
        params["config"] = config
    # A filter-insights count is scoped to ONE prompt version, because a
    # prompt_hash IS the filter as it was actually run - the same name has been
    # two different prompts. A drill-through that carried only the reason group
    # would open that group across every version, so the count and the rows it
    # opened could not match, and not by a little.
    if prompt_hash:
        clauses.append("prompt_hash = %(prompt_hash)s")
        params["prompt_hash"] = prompt_hash
    if url:
        clauses.append("url = %(url)s")
        params["url"] = url
    if q:
        # Default search hits only trigram-indexed columns; including
        # input_content (unindexed page dumps) forces a sequential scan, so
        # it's opt-in via deep=true.
        cols = (
            "(reason ILIKE %(q)s OR url ILIKE %(q)s OR company ILIKE %(q)s OR job_title ILIKE %(q)s"
        )
        if deep:
            cols += " OR input_content ILIKE %(q)s"
        clauses.append(cols + ")")
        params["q"] = f"%{q}%"
    # The filter-insights aggregate classifies reasons in Python; this
    # drill-through has to reproduce that selection in SQL. If the two ever
    # disagreed, a count would link to a different set of rows than it counted,
    # which is worse than not linking at all - so both spellings are generated
    # from core.reason_taxonomy and pinned equal by a test.
    #
    # `~*` is never itself indexed: the trigram index on `reason` cannot serve
    # a general regex (GIN trigram covers LIKE and similarity only), so do not
    # read that index's existence as meaning this is.
    #
    # What it scans depends on what it is composed with. A drill-through link
    # carries check_type and prompt_hash, and idx_ai_queries_prompt_hash is on
    # (check_type, prompt_hash) - so those two select the rows first and the
    # regex only runs over that one prompt version. Used alone, it scans all
    # 78k, which is still only tens of milliseconds.
    if reason_group:
        clauses.append("reason ~* %(reason_group)s")
        params["reason_group"] = reason_taxonomy.sql_pattern(reason_group)
    if evidence_missing:
        clauses.append("reason ~* %(evidence_missing)s")
        params["evidence_missing"] = reason_taxonomy.EVIDENCE_MISSING_SQL
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


class QueryFilterOptions(BaseModel):
    """The dropdowns, generated from live data so they cannot drift from what
    exists. `contexts` and `workers` are narrowed to the last 30 days: a value
    nothing has produced in a month is a dead option, not a filter."""

    sources: list[str]
    check_types: list[str]
    statuses: list[str]
    contexts: list[str]
    workers: list[str]


@router.get("/queries/options")
def query_options(user: AuthedUser = Depends(require_admin)) -> QueryFilterOptions:
    """Filter vocabularies generated from live data, so the admin dropdowns
    can never drift from what actually exists."""

    def col(sql: str, key: str) -> list[str]:
        return [r[key] for r in db.query(sql) if r[key]]

    return QueryFilterOptions(
        sources=col("SELECT name FROM sources WHERE active ORDER BY name", "name"),
        check_types=col(
            "SELECT DISTINCT check_type FROM ai_queries WHERE check_type IS NOT NULL "
            "ORDER BY check_type",
            "check_type",
        ),
        statuses=col(
            "SELECT DISTINCT status FROM ai_queries WHERE status IS NOT NULL ORDER BY status",
            "status",
        ),
        # Pipeline context (which code path decided), not the dead legacy
        # config names: only values seen in the last 30 days.
        contexts=col(
            "SELECT DISTINCT config_name FROM ai_queries WHERE config_name IS NOT NULL "
            "AND created_at > now() - interval '30 days' ORDER BY config_name",
            "config_name",
        ),
        workers=col(
            "SELECT DISTINCT worker FROM ai_queries WHERE worker IS NOT NULL "
            "AND created_at > now() - interval '30 days' ORDER BY worker",
            "worker",
        ),
    )


class QueryListing(BaseModel):
    """`_LIST_COLS`, in types: one model call as the ledger lists it.

    `instructions`, `input_content` and `parsed_json` are deliberately absent.
    They are page dumps and prompts, several kilobytes each, and only the row
    a person opens needs them; GET /admin/queries/{id} carries those.

    `job_id` and `source` are looked up per row rather than joined: ai_queries
    and jobs share several column names, so a join would make every existing
    filter ambiguous. A verdict about a url the catalog no longer holds has
    neither.
    """

    id: int
    created_at: datetime.datetime
    config_name: str | None
    url: str | None
    check_type: str | None
    status: str | None
    reason: str | None
    model: str | None
    company: str | None
    job_title: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    duration_ms: int | None
    error: str | None
    worker: str | None
    filter_name: str | None
    prompt_hash: str | None
    job_id: int | None
    source: str | None


class QueryPage(BaseModel):
    sorts: list[dict[str, str]]
    sortable: list[str]
    rows: list[QueryListing]
    page: int
    page_size: int
    total: int
    has_more: bool
    filters: dict[str, list[str]]
    filterable: list[str]


@router.get("/queries")
def list_queries(
    check_type: str | None = None,
    status: str | None = None,
    config: str | None = None,
    url: str | None = None,
    q: str | None = None,
    deep: bool = False,
    sources: str | None = None,
    reason_group: str | None = None,
    evidence_missing: bool = False,
    prompt_hash: str | None = None,
    users: str | None = Query(default=None, alias="user"),
    sort: str = "id",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
) -> QueryPage:
    ids = scoping.user_ids(users)
    try:
        where, params = _where(
            check_type,
            status,
            config,
            url,
            q,
            deep,
            sources,
            reason_group,
            evidence_missing,
            prompt_hash,
            user_ids=ids,
        )
    except KeyError:
        # An unknown group key is a bad request, not a server fault. The keys
        # are a closed server-side vocabulary, so a caller sending one that
        # does not exist has a stale link, and should be told which.
        raise HTTPException(
            400,
            detail={
                "code": "UNKNOWN_REASON_GROUP",
                "message": f"unknown reason_group: {reason_group}",
                "valid": [g.key for g in reason_taxonomy.GROUPS],
            },
        ) from None
    paging = pagination.Page.from_params(page, page_size, maximum=500)
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM ai_queries {where}", params)
    sortable = {key: key for key in _SORTABLE}
    sorts = sorting.parse(sort, dir, sortable, "id")
    rows = db.query_as(
        QueryListing,
        f"SELECT {_LIST_COLS} FROM ai_queries {where} "
        f"ORDER BY {sorting.clause(sorts, sortable)}, id DESC LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": paging.size, "offset": paging.offset},
    )
    total = total_row["c"] if total_row else 0
    return QueryPage(
        sorts=sorts,
        sortable=sorted(sortable),
        rows=rows,
        **paging.metadata(total),
        filters=params_.applied(
            check_type=params_.csv(check_type),
            status=params_.csv(status),
            config=params_.csv(config),
            sources=params_.csv(sources),
            prompt_hash=params_.csv(prompt_hash),
            reason_group=params_.csv(reason_group),
            user=scoping.echo(ids),
        ),
        filterable=_QUERIES_FILTERABLE,
    )


_QUERIES_FILTERABLE = [
    "check_type",
    "status",
    "config",
    "url",
    "q",
    "sources",
    "reason_group",
    "evidence_missing",
    "prompt_hash",
    "user",
]


# Every column of ai_queries, named once. Two routes returned the whole row
# through SELECT *, which cannot be typed and drifts from whatever reads it.
_ROW_COLS = (
    "id, created_at, config_name, url, check_type, status, reason, model, "
    "reasoning_effort, filter_name, prompt_hash, company, job_title, instructions, "
    "input_content, parsed_json, prompt_tokens, completion_tokens, total_tokens, "
    "cached_tokens, reasoning_tokens, duration_ms, error, cost_usd, worker, batch_id"
)


class QueryRecord(BaseModel):
    """One model call, whole: what was asked, what came back, and what it
    cost. `instructions` and `input_content` are the prompt and the page as
    they were at the time, which is what makes a verdict re-readable rather
    than merely re-runnable. `parsed_json` is the answer before it was reduced
    to status and reason.

    `cost_usd` is a float rather than a Decimal because a declared Decimal
    serialises as a JSON string and this has always been a number.
    """

    id: int
    created_at: datetime.datetime
    config_name: str | None
    url: str | None
    check_type: str | None
    status: str | None
    reason: str | None
    model: str | None
    reasoning_effort: str | None
    filter_name: str | None
    prompt_hash: str | None
    company: str | None
    job_title: str | None
    instructions: str | None
    input_content: str | None
    parsed_json: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    duration_ms: int | None
    error: str | None
    cost_usd: float | None
    worker: str | None
    batch_id: str | None


@router.get("/queries/{query_id}")
def get_query(query_id: int, user: AuthedUser = Depends(require_admin)) -> QueryRecord:
    row = db.query_one_as(
        QueryRecord, f"SELECT {_ROW_COLS} FROM ai_queries WHERE id = %s", (query_id,)
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown query"})
    return row


class DeleteQueries(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=10000)


class QueriesDeleted(BaseModel):
    deleted: int


@router.post("/queries/delete")
def delete_queries(
    body: DeleteQueries, user: AuthedUser = Depends(require_admin)
) -> QueriesDeleted:
    with db.pool.connection() as conn:
        result = conn.execute("DELETE FROM ai_queries WHERE id = ANY(%s)", (body.ids,))
        deleted = result.rowcount
    return QueriesDeleted(deleted=deleted)


# Sortable columns for the job aggregate. Values are looked up here, never
# interpolated from the request, which is what keeps the ORDER BY safe.
_JOBS_SORTABLE = {
    "last_seen": "last_seen",
    "company": "lower(MAX(company))",
    "job_title": "lower(MAX(job_title))",
    "checks": "checks",
    "passed": "passed",
    "rejected": "rejected",
    "failed": "failed",
    "total_tokens": "total_tokens",
    "url": "url",
}


class CheckedPosting(BaseModel):
    """Every check ever run against one url, rolled up. The company and title
    are MAX() over the rows rather than a join to the catalog: this is the
    verdict ledger's own account of a posting, and it answers for urls the
    catalog no longer holds.

    `verdict` is the roll-up of the roll-up: one rejection anywhere makes the
    posting rejected, since a posting that fails any check is out.
    """

    url: str
    company: str | None
    job_title: str | None
    config_name: str | None
    checks: int
    passed: int
    rejected: int
    failed: int
    total_tokens: int
    last_seen: datetime.datetime
    verdict: str


class CheckedPostings(BaseModel):
    rows: list[CheckedPosting]
    page: int
    page_size: int
    total: int
    has_more: bool
    sort: str
    dir: str
    sorts: list[dict[str, str]]
    sortable: list[str]


@router.get("/jobs")
def list_jobs(
    q: str | None = None,
    config: str | None = None,
    verdict: str | None = None,
    sources: str | None = None,
    sort: str = "last_seen",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
) -> CheckedPostings:
    paging = pagination.Page.from_params(page, page_size, maximum=500)
    sub = ["url IS NOT NULL"]
    params: dict = {}
    wanted_sources = [s.strip() for s in (sources or "").split(",") if s.strip()]
    if wanted_sources:
        # Same shape as _where(): ai_queries is keyed by url and source lives on
        # the job, so a subquery keeps this composable with the count and page
        # queries below rather than making every other filter ambiguous.
        sub.append("url IN (SELECT url FROM jobs WHERE source = ANY(%(sources)s))")
        params["sources"] = wanted_sources
    if q:
        sub.append(
            "(url LIKE %(q)s OR company LIKE %(q)s OR job_title LIKE %(q)s OR reason LIKE %(q)s)"
        )
        params["q"] = f"%{q}%"
    if config:
        sub.append("config_name = %(config)s")
        params["config"] = config
    having = ""
    if verdict == "rejected":
        having = "HAVING SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) > 0"
    elif verdict == "passed":
        having = (
            "HAVING SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) = 0 "
            "AND SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) > 0"
        )
    base = f"""
        SELECT url,
            MAX(company) AS company,
            MAX(job_title) AS job_title,
            MAX(config_name) AS config_name,
            COUNT(*) AS checks,
            SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            MAX(created_at) AS last_seen
        FROM ai_queries
        WHERE url IN (SELECT url FROM ai_queries WHERE {" AND ".join(sub)})
        GROUP BY url
        {having}
    """
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM ({base}) sub", params)
    sorts = sorting.parse(sort, dir, _JOBS_SORTABLE, "last_seen")
    rows = db.query(
        f"{base} ORDER BY {sorting.clause(sorts, _JOBS_SORTABLE)}, url "
        "LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": paging.size, "offset": paging.offset},
    )
    total = total_row["c"] if total_row else 0
    return CheckedPostings(
        rows=[
            CheckedPosting(
                **r,
                verdict="rejected"
                if r["rejected"] > 0
                else "passed"
                if r["passed"] > 0
                else "other",
            )
            for r in rows
        ],
        **paging.metadata(total),
        # Echoed so the UI can render the active sort without duplicating the
        # default, and sortable so it never has to guess the accepted keys.
        sort=sorts[0]["key"],
        dir=sorts[0]["dir"],
        sorts=sorts,
        sortable=sorted(_JOBS_SORTABLE),
    )


class QueryResponses(BaseModel):
    """Every model call against one url, whole rows, oldest first. This is the
    drawer that shows what was asked and what came back, so it carries the
    prompt and the page text the list deliberately leaves out."""

    rows: list[QueryRecord]


@router.get("/jobs/responses")
def job_responses(url: str, user: AuthedUser = Depends(require_admin)) -> QueryResponses:
    return QueryResponses(
        rows=db.query_as(
            QueryRecord,
            f"SELECT {_ROW_COLS} FROM ai_queries WHERE url = %s ORDER BY id ASC",
            (url,),
        )
    )


class TimelineEntry(BaseModel):
    """One check in a posting's history, thin enough to render as a line. The
    interesting column is `reason`: a posting's story is the sequence of
    verdicts and why each one was given."""

    id: int
    created_at: datetime.datetime
    config_name: str | None
    check_type: str | None
    status: str | None
    reason: str | None
    model: str | None
    total_tokens: int | None
    duration_ms: int | None
    error: str | None


class PostingTimeline(BaseModel):
    rows: list[TimelineEntry]


@router.get("/jobs/timeline")
def job_timeline(url: str, user: AuthedUser = Depends(require_admin)) -> PostingTimeline:
    return PostingTimeline(
        rows=db.query_as(
            TimelineEntry,
            "SELECT id, created_at, config_name, check_type, status, reason, model, "
            "total_tokens, duration_ms, error "
            "FROM ai_queries WHERE url = %s ORDER BY id ASC",
            (url,),
        )
    )


class QueryVocabulary(BaseModel):
    """The older, unscoped vocabulary: every distinct value ever recorded.
    GET /admin/queries/options is the same idea narrowed to what is still in
    use, and is what the queries page reads."""

    check_types: list[str]
    statuses: list[str]
    configs: list[str]


@router.get("/options")
def options(user: AuthedUser = Depends(require_admin)) -> QueryVocabulary:
    def distinct(col: str) -> list[str]:
        return [
            r["v"]
            for r in db.query(
                f"SELECT DISTINCT {col} AS v FROM ai_queries WHERE {col} IS NOT NULL ORDER BY {col}"
            )
        ]

    return QueryVocabulary(
        check_types=distinct("check_type"),
        statuses=distinct("status"),
        configs=distinct("config_name"),
    )


class LedgerTotals(BaseModel):
    """Lifetime totals over every model call. `cost_usd` is not a column: it
    is the sum of what each model's tokens priced at, and a model that cannot
    be priced contributes nothing to it rather than a guess."""

    queries: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float


class CheckTypeTotals(BaseModel):
    check_type: str | None
    count: int
    prompt_tokens: int
    completion_tokens: int


class StatusTotals(BaseModel):
    status: str | None
    count: int


class DayTotals(BaseModel):
    """One calendar day in the session timezone, not the first ten characters
    of a timestamp."""

    day: datetime.date
    queries: int
    failed: int
    rejected: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int


class ModelTotals(BaseModel):
    """What one model has been asked and what that cost.

    The batched columns are carried because batched and synchronous tokens
    bill at different rates and are priced as two calls: one blended rate over
    the whole model would be wrong by up to 2x depending on the mix.

    `cost_usd` is null where the model cannot be priced from summed tokens: a
    tiered model's rate depends on each individual request's prompt length, so
    a thousand small calls sum into a tier none of them was billed at, and the
    honest answer is that it is not priced.
    """

    model: str
    queries: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    batched_prompt_tokens: int
    batched_completion_tokens: int
    batched_cached_tokens: int
    cost_usd: float | None


class LedgerStats(BaseModel):
    totals: LedgerTotals
    by_check_type: list[CheckTypeTotals]
    by_status: list[StatusTotals]
    by_day: list[DayTotals]
    by_model: list[ModelTotals]


_stats_cache: tuple[float, LedgerStats] | None = None


@router.get("/stats")
def stats(user: AuthedUser = Depends(require_admin)) -> LedgerStats:
    """Lifetime totals over ai_queries, served from a per-process cache for
    admin_stats_cache_seconds. Every call is five full scans of the largest
    table, and the dashboard asks on every worker event; the totals move by
    a few rows an hour. ponytail: per-process cache, so each api replica
    scans once per window; shared cache if replicas multiply."""
    global _stats_cache
    ttl = int(db.get_config("admin_stats_cache_seconds"))
    if _stats_cache and time.monotonic() - _stats_cache[0] < ttl:
        return _stats_cache[1]
    result = _compute_stats()
    _stats_cache = (time.monotonic(), result)
    return result


def _compute_stats() -> LedgerStats:
    totals = db.query_one(
        """
        SELECT COUNT(*) AS queries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
        FROM ai_queries
        """
    )
    by_check_type = db.query_as(
        CheckTypeTotals,
        """
        SELECT check_type, COUNT(*) AS count,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens
        FROM ai_queries GROUP BY check_type ORDER BY count DESC
        """,
    )
    by_status = db.query_as(
        StatusTotals,
        "SELECT status, COUNT(*) AS count FROM ai_queries GROUP BY status ORDER BY count DESC",
    )
    by_day = db.query_as(
        DayTotals,
        """
        -- created_at is timestamptz since a7c1e9d40b22; substr() has no
        -- overload for it. ::date also makes the bucket a real calendar day
        -- in the session timezone rather than the first ten characters of
        -- whatever string the writer happened to produce.
        SELECT created_at::date AS day,
               COUNT(*) AS queries,
               COUNT(*) FILTER (WHERE status = 'failed') AS failed,
               COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
        FROM ai_queries GROUP BY day ORDER BY day ASC
        """,
    )
    # Cost is computed here, not in the browser. The client had one hardcoded
    # gpt-5-nano price applied to every token, but PRICES_PER_MTOK spans
    # $0.05-$5.00 per Mtok - a 100x range - so the headline number was wrong
    # the moment anything ran on a different model, and silently so.
    # Batched calls bill at half price, which the client could not know either.
    by_model = db.query(
        """
        SELECT model,
               COUNT(*) AS queries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(prompt_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_prompt_tokens,
               COALESCE(SUM(completion_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_completion_tokens,
               COALESCE(SUM(cached_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_cached_tokens
        FROM ai_queries WHERE model IS NOT NULL GROUP BY model ORDER BY queries DESC
        """
    )
    total_cost = Decimal(0)
    priced: list[ModelTotals] = []
    for row in by_model:
        cost_usd = None
        if not pricing.is_tiered(row["model"]):
            # Batched and synchronous tokens bill at different rates, so they
            # are priced as two separate calls and summed - one blended rate
            # over the whole model would be wrong by up to 2x depending on the
            # mix.
            batched = pricing.estimate_cost_usd(
                row["model"],
                row["batched_prompt_tokens"],
                row["batched_completion_tokens"],
                cached_tokens=row["batched_cached_tokens"],
                batched=True,
            )
            sync = pricing.estimate_cost_usd(
                row["model"],
                int(row["prompt_tokens"]) - int(row["batched_prompt_tokens"]),
                int(row["completion_tokens"]) - int(row["batched_completion_tokens"]),
                cached_tokens=int(row["cached_tokens"]) - int(row["batched_cached_tokens"]),
            )
            if batched is not None and sync is not None:
                cost = batched + sync
                cost_usd = round(float(cost), 6)
                total_cost += cost
        priced.append(ModelTotals(**row, cost_usd=cost_usd))

    # COUNT(*) with no GROUP BY, so there is always exactly one row.
    assert totals is not None
    return LedgerStats(
        totals=LedgerTotals(**totals, cost_usd=round(float(total_cost), 6)),
        by_check_type=by_check_type,
        by_status=by_status,
        by_day=by_day,
        by_model=priced,
    )
