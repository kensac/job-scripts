"""Admin model-call ledger rows and filters."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api import db, pagination, scoping, sorting
from api import params as params_
from api.auth import AuthedUser
from api.orm.ai import AiQuery
from api.problem import refuse
from api.routers.admin.shared import require_admin
from api.stale_cache import Stale
from core import answer_inputs, page_fetches, query_instructions, reason_taxonomy

router = APIRouter()


_QUERY_SORT_COLUMNS = {
    "id",
    "created_at",
    "check_type",
    "status",
    "company",
    "total_tokens",
    "duration_ms",
}
# Read off the model, which CI holds equal to the migrations: a sort key here
# loses its NULLS LAST (see sorting.clause) only while its column cannot be NULL.
_QUERY_NON_NULL_SORT_COLUMNS = (
    frozenset(c.name for c in AiQuery.__table__.columns if not c.nullable) & _QUERY_SORT_COLUMNS
)


_LIST_COLS = (
    "id, created_at, config_name, url, check_type, status, reason, model, "
    "company, job_title, prompt_tokens, completion_tokens, total_tokens, "
    "cached_tokens, cache_write_tokens, reasoning_tokens, duration_ms, error, worker, "
    "filter_name, prompt_hash, "
    # Correlated lookups rather than a join: ai_queries and jobs share several
    # column names, so joining would make every existing filter ambiguous.
    "(SELECT j.id FROM jobs j WHERE j.url = ledger_rows.url) AS job_id, "
    "(SELECT j.source FROM jobs j WHERE j.url = ledger_rows.url) AS source"
)


def _query_filter_clause(
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
    wanted_sources = params_.csv(sources)
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
        # A sequential scan of ai_queries: company and job_title have no
        # trigram index (7c0b33a7fd95 dropped them unscanned), and an OR is
        # index-backed only when every arm is. deep=true adds input_content,
        # the page dumps, which multiplies what that scan reads. A page held
        # by reference (core.page_fetches) is not in the database to match.
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
    # `~*` can use the trigram index on `reason`: GIN trigram extracts the
    # trigrams a regex requires. Whether the planner takes it depends on the
    # pattern; on a 1M-row test copy (2026-10-03) seven of the nine groups
    # and evidence_missing used idx_ai_queries_reason_trgm.
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


# What each vocabulary column is in ledger_rows' fetch arm: a column of
# page_fetches, or the constant the view puts there.
_FETCH_ARM = {
    "check_type": "'content'",
    "config_name": "'content-cache'",
    "status": "status",
    "worker": "worker",
}


def _skip_scan(table: str, col: str) -> str:
    """The distinct non-null values of `col`, one index descent each."""
    return f"""
        SELECT x FROM (
            WITH RECURSIVE v(x) AS (
                SELECT min({col}) FROM {table}
                UNION ALL
                SELECT (SELECT min({col}) FROM {table} WHERE {col} > v.x)
                FROM v WHERE v.x IS NOT NULL
            )
            SELECT x FROM v
        ) {table}_values WHERE x IS NOT NULL
    """


def _distinct_query_values(col: str, recent: bool = False) -> list[str]:
    """`SELECT DISTINCT col FROM ledger_rows WHERE col IS NOT NULL ORDER BY
    col`, as a skip scan: each step asks the index for the next value above the
    last, so the cost is one descent per distinct value instead of a read of
    every row. `recent` keeps a value only if a row in the last 30 days has
    it, which on an index over (col, created_at) is an equality plus a range,
    one more descent. Every column passed here needs an index that leads with
    it on ai_queries, and on page_fetches when the fetch arm reads a column.

    The two arms of ledger_rows are scanned on their own tables: through the
    view the fetch arm is not flattened, so each step sorted every fetch and
    the four vocabularies took 30 s on production (2026-10-13). A fetch value
    counts only if a fetch other than a verification copy carries it, as in
    the view. Measured before that, against 2.07M rows (2026-10-04, warm):
    DISTINCT check_type 2.0 s became 66 ms, status 0.3 s became 3 ms. `col` is
    a literal in this module, never request input.
    """
    window = "AND r.created_at > now() - interval '30 days'" if recent else ""
    fetch = _FETCH_ARM[col]
    if fetch == col:
        fetch_values = f"""
            SELECT x FROM ({_skip_scan("page_fetches", col)}) f
            WHERE EXISTS (SELECT 1 FROM page_fetches r WHERE r.{col} = f.x
                          AND r.method <> 'verification' {window})
        """
    else:
        fetch_values = f"""
            SELECT {fetch} WHERE EXISTS (
                SELECT 1 FROM page_fetches r WHERE r.method <> 'verification' {window})
        """
    return [
        r["x"]
        for r in db.query(
            f"""
            SELECT x FROM ({_skip_scan("ai_queries", col)}) a
            WHERE EXISTS (SELECT 1 FROM ai_queries r WHERE r.{col} = a.x {window})
            UNION
            {fetch_values}
            ORDER BY x
            """
        )
    ]


@router.get("/queries/options")
def query_options(user: AuthedUser = Depends(require_admin)) -> QueryFilterOptions:
    """Filter vocabularies generated from live data, so the admin dropdowns
    can never drift from what actually exists. Served stale for
    admin_stats_cache_seconds: the skip scans through ledger_rows took 30 s
    on production (2026-10-13), and a new value appears a few times a month."""
    return _options.get()


def _compute_options() -> QueryFilterOptions:
    # An empty string is no option here; GET /options has always listed it.
    sources = [r["name"] for r in db.query("SELECT name FROM sources WHERE active ORDER BY name")]
    return QueryFilterOptions(
        sources=[v for v in sources if v],
        check_types=[v for v in _distinct_query_values("check_type") if v],
        statuses=[v for v in _distinct_query_values("status") if v],
        # Pipeline context (which code path decided), not the dead legacy
        # config names: only values seen in the last 30 days.
        contexts=[v for v in _distinct_query_values("config_name", recent=True) if v],
        workers=[v for v in _distinct_query_values("worker", recent=True) if v],
    )


_options = Stale(_compute_options, lambda: int(db.get_config("admin_stats_cache_seconds")))


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
    cache_write_tokens: int | None
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
        where, params = _query_filter_clause(
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
        raise refuse(
            400,
            "UNKNOWN_REASON_GROUP",
            f"unknown reason_group: {reason_group}",
            valid=[g.key for g in reason_taxonomy.GROUPS],
        ) from None
    paging = pagination.Page.from_params(page, page_size, maximum=500)
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM ledger_rows {where}", params)
    sortable = {key: key for key in _QUERY_SORT_COLUMNS}
    sorts = sorting.parse(sort, dir, sortable, "id")
    rows = db.query_as(
        QueryListing,
        f"SELECT {_LIST_COLS} FROM ledger_rows {where} "
        f"ORDER BY {sorting.clause(sorts, sortable, _QUERY_NON_NULL_SORT_COLUMNS)}, id DESC LIMIT %(limit)s OFFSET %(offset)s",
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
# The instructions come from instructions_id through query_instructions.hydrate.
_QUERY_DETAIL_COLUMNS = (
    "id, created_at, config_name, url, check_type, status, reason, model, "
    "reasoning_effort, filter_name, prompt_hash, company, job_title, "
    "input_content, parsed_json, prompt_tokens, completion_tokens, total_tokens, "
    "cached_tokens, cache_write_tokens, reasoning_tokens, duration_ms, error, cost_usd, worker, batch_id"
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
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    duration_ms: int | None
    error: str | None
    cost_usd: float | None
    worker: str | None
    batch_id: str | None


def query_records(where: str, params: tuple) -> list[QueryRecord]:
    """Whole model calls from ledger_rows, `where` being the WHERE clause and
    any ORDER BY. A page held by reference is read back into input_content."""
    rows = db.query(
        f"SELECT {_QUERY_DETAIL_COLUMNS}, instructions_id FROM ledger_rows WHERE {where}", params
    )
    # ponytail: one lookup per held row; batch it if a posting's history grows
    # past a drawer's worth of calls.
    for row in rows:
        if row["input_content"] is None:
            row["input_content"] = _held_input(row)
    return [QueryRecord.model_validate(row) for row in query_instructions.hydrate(rows)]


@router.get("/queries/{query_id}")
def get_query(query_id: int, user: AuthedUser = Depends(require_admin)) -> QueryRecord:
    records = query_records("id = %s", (query_id,))
    if not records:
        raise refuse(404, "NOT_FOUND", "unknown query")
    return records[0]


def _held_input(row: dict) -> str | None:
    """The input of a row whose page text is held by reference, which
    ledger_rows cannot rebuild in SQL: a fetch row is its own text, and an
    answer's is core.answer_inputs over its fetch's text, as the view does."""
    if row["config_name"] == "content-cache":
        return page_fetches.text_of(row["id"])
    answer = db.query_one("SELECT page_fetch_id FROM ai_queries WHERE id = %s", (row["id"],))
    fetch_id = answer["page_fetch_id"] if answer else None
    text = page_fetches.text_of(fetch_id) if fetch_id is not None else None
    if text is None:
        return None
    rebuilt = db.query_one(
        f"SELECT {answer_inputs.sql('q', '%(text)s::text')} AS input "
        "FROM ai_queries q WHERE q.id = %(id)s",
        {"text": text, "id": row["id"]},
    )
    return rebuilt["input"] if rebuilt else None


class DeleteQueries(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=10000)


class QueriesDeleted(BaseModel):
    deleted: int


@router.post("/queries/delete")
def delete_queries(
    body: DeleteQueries, user: AuthedUser = Depends(require_admin)
) -> QueriesDeleted:
    with db.pool.connection() as conn:
        deleted = conn.execute("DELETE FROM ai_queries WHERE id = ANY(%s)", (body.ids,)).rowcount
        deleted += conn.execute("DELETE FROM page_fetches WHERE id = ANY(%s)", (body.ids,)).rowcount
    return QueriesDeleted(deleted=deleted)
