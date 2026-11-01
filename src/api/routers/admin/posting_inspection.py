"""Admin posting rollups, call responses, and timeline inspection."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db, pagination, posting_path, sorting
from api import params as params_
from api.ai import verdicts
from api.auth import AuthedUser
from api.board import column_filters as column_filters_
from api.problem import refuse
from api.routers.admin.query_ledger import _QUERY_DETAIL_COLUMNS, QueryRecord, _held_input
from api.routers.admin.shared import require_admin
from api.stale_cache import Stale, StaleByKey
from core import catalog, query_instructions, verdict_reads

router = APIRouter()


def _count(sql: str) -> int:
    row = db.query_one(sql)
    return row["c"] if row else 0


# Every url the ledger has checked, for the unfiltered page's total: a
# distinct count over both arms of ledger_rows, ~40 s on production under load
# (2026-10-13), and it moves by a few hundred an hour.
_CHECKED_URLS_COUNT = "SELECT COUNT(DISTINCT url) AS c FROM ledger_rows WHERE url IS NOT NULL"
_checked_urls = Stale(
    lambda: _count(_CHECKED_URLS_COUNT),
    lambda: int(db.get_config("admin_stats_cache_seconds")),
)

# A posting's verdict is the roll-up of its checks: one rejection anywhere
# makes it rejected, and passed is a pass with no rejection. page_fetches
# rows are only ever passed or failed (its CHECK constraint), so a rejection
# lives in ai_queries alone. These are the list's HAVING clauses, read for
# one url at a time (u.url) instead of grouping the whole ledger.
_REJECTED = "EXISTS (SELECT 1 FROM ai_queries q WHERE q.url = u.url AND q.status = 'rejected')"
_PASSED = """(EXISTS (SELECT 1 FROM ai_queries q WHERE q.url = u.url AND q.status = 'passed')
    OR EXISTS (SELECT 1 FROM page_fetches f WHERE f.url = u.url AND f.status = 'passed'
               AND f.method <> 'verification'))"""
_VERDICT_HOLDS = {"rejected": _REJECTED, "passed": f"NOT {_REJECTED} AND {_PASSED}"}

# Postings with neither verdict: every check failed or has no status. They
# are few (5,870 of 1,279,002 on 2026-11-01), found from the rows that are
# neither.
_NO_VERDICT_COUNT = f"""
    SELECT COUNT(*) AS c FROM (
        SELECT url FROM ai_queries
        WHERE url IS NOT NULL
          AND status IS DISTINCT FROM 'passed' AND status IS DISTINCT FROM 'rejected'
        UNION
        SELECT url FROM page_fetches
        WHERE status = 'failed' AND url IS NOT NULL AND method <> 'verification'
    ) u
    WHERE NOT {_REJECTED} AND NOT {_PASSED}
"""
_REJECTED_COUNT = (
    "SELECT COUNT(DISTINCT url) AS c FROM ai_queries WHERE status = 'rejected' AND url IS NOT NULL"
)


def _verdict_total(verdict: str) -> int:
    """Rejected is counted. Passed is every checked url less the rejected and
    the ones with no verdict: counting it directly took 3 to 4.5 min on
    production, against 11.6 s and 17 s for the two it subtracts, and the
    three add up exactly (1,279,002 = 517,274 + 755,858 + 5,870 on
    2026-11-01). Each count is read here, in one call, so they agree."""
    rejected = _count(_REJECTED_COUNT)
    if verdict == "rejected":
        return rejected
    return _count(_CHECKED_URLS_COUNT) - rejected - _count(_NO_VERDICT_COUNT)


# The Passed and Rejected tabs' totals, lifetime counts like _checked_urls.
# Through the list's HAVING the rejected count alone took 66 s on production
# (2026-11-01), so the tab answered 503 before its page query started.
_verdict_totals: StaleByKey[str, int] = StaleByKey(
    lambda: int(db.get_config("admin_stats_cache_seconds"))
)


# Sortable columns for the job aggregate. Values are looked up here, never
# interpolated from the request, which is what keeps the ORDER BY safe.
_POSTING_SORT_EXPRESSIONS = {
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
    posting rejected, since a posting that fails any check is out. A posting
    with no verdict whose page fetch has failed fetch_give_up_after_failures
    times running is `unfetchable` rather than `other`: nothing automatic
    will fetch it again until an admin re-check succeeds.

    `content_failures` is the run of empty fetches since the last success,
    which is what the backoff and the give-up read; `failed` counts every
    failed row of any check, ever.
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
    content_failures: int
    unfetchable: bool


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


_POSTING_TOTALS = """
    MAX(company) AS company,
    MAX(job_title) AS job_title,
    MAX(config_name) AS config_name,
    COUNT(*) AS checks,
    SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS passed,
    SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
    COALESCE(SUM(total_tokens), 0) AS total_tokens,
    MAX(created_at) AS last_seen
"""


# The newest postings, as urls in order. Walking the view newest first and
# keeping a row only when it is its url's newest made each candidate a
# correlated lookup, and the fetch arm ran it on all 955k fetches before it
# could sort: 2.5 min and more on production (2026-10-13). Rows newest first
# are cheap (each arm walks its created_at index), so read a window of them,
# keep each url's first appearance, and widen the window until the page is
# full or the ledger runs out. The two arms of ledger_rows are named here
# because the view's fetch arm was not flattened then (before 58e4e8b6b54d),
# so through the view it was a sort of every fetch (4.3 s) rather than a walk
# of its index.
_NEWEST_ROWS = """
    (SELECT url, created_at FROM ai_queries WHERE url IS NOT NULL
     ORDER BY created_at DESC LIMIT %(n)s)
    UNION ALL
    (SELECT url, created_at FROM page_fetches
     WHERE url IS NOT NULL AND method <> 'verification'
     ORDER BY created_at DESC LIMIT %(n)s)
    ORDER BY created_at DESC, url LIMIT %(n)s
"""


def _newest_urls(offset: int, limit: int, verdict: str | None = None) -> list[str]:
    """With a verdict, a url is kept only when it holds that verdict, checked
    against its whole history; the window widens until the page is full."""
    want = offset + limit
    n = max(want * 4, 200)
    checked: set[str] = set()
    held: set[str] = set()
    while True:
        rows = db.query(_NEWEST_ROWS, {"n": n})
        urls = list(dict.fromkeys(r["url"] for r in rows))
        if verdict is not None:
            # A wider window repeats the narrower one; each url is checked once.
            unchecked = [url for url in urls if url not in checked]
            held.update(
                r["url"]
                for r in db.query(
                    "SELECT u.url FROM unnest(%(urls)s::text[]) AS u(url) "
                    f"WHERE {_VERDICT_HOLDS[verdict]}",
                    {"urls": unchecked},
                )
            )
            checked.update(unchecked)
            urls = [url for url in urls if url in held]
        if len(urls) >= want or len(rows) < n:
            return urls[offset:want]
        n *= 4


_RECENT_POSTINGS = f"""
    SELECT selected.url, totals.*, streak.failures AS content_failures
    FROM unnest(%(urls)s::text[]) WITH ORDINALITY AS selected(url, position)
    CROSS JOIN LATERAL (
        SELECT {_POSTING_TOTALS} FROM ledger_rows WHERE url = selected.url
    ) totals
    CROSS JOIN LATERAL ({verdicts.FETCH_STREAK.format(url="selected.url")}) streak
    ORDER BY selected.position
"""


_URL_STREAK = verdicts.FETCH_STREAK.format(url="ledger_rows.url")


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
    wanted_sources = params_.csv(sources)
    if wanted_sources:
        # Same shape as _query_filter_clause(): ai_queries is keyed by url and source lives on
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
    # The count below wraps this, and the planner drops a subquery output
    # nobody reads, so the streak is paid for only on the page.
    base = f"""
        SELECT url, {_POSTING_TOTALS},
            (SELECT streak.failures FROM ({_URL_STREAK}) streak) AS content_failures
        FROM ledger_rows
        WHERE url IN (SELECT url FROM ledger_rows WHERE {" AND ".join(sub)})
        GROUP BY url
        {having}
    """
    sorts = sorting.parse(sort, dir, _POSTING_SORT_EXPRESSIONS, "last_seen")
    page_params = {**params, "limit": paging.size, "offset": paging.offset}
    if len(sub) == 1 and sorts == [{"key": "last_seen", "dir": "desc"}]:
        # Walk the time index to find the page, then read history only for
        # those URLs. created_at is NOT NULL, so DESC has the same ordering
        # as DESC NULLS LAST and can use the existing index backwards.
        # The id tiebreaker selects one row when a URL has equal timestamps;
        # every check still contributes to that URL's reported totals.
        known = verdict if verdict in _VERDICT_HOLDS else None
        total_row = {
            "c": _checked_urls.get()
            if known is None
            else _verdict_totals.get(known, lambda: _verdict_total(known))
        }
        urls = _newest_urls(paging.offset, paging.size, known)
        rows = db.query(_RECENT_POSTINGS, {"urls": urls}) if urls else []
    else:
        total_row = db.query_one(f"SELECT COUNT(*) AS c FROM ({base}) sub", params)
        rows = db.query(
            f"{base} ORDER BY {sorting.clause(sorts, _POSTING_SORT_EXPRESSIONS)}, url "
            "LIMIT %(limit)s OFFSET %(offset)s",
            page_params,
        )
    total = total_row["c"] if total_row else 0
    give_up = int(db.get_config("fetch_give_up_after_failures"))
    return CheckedPostings(
        rows=[
            CheckedPosting(
                **r,
                verdict="rejected"
                if r["rejected"] > 0
                else "passed"
                if r["passed"] > 0
                else "unfetchable"
                if r["content_failures"] >= give_up
                else "other",
                unfetchable=r["content_failures"] >= give_up,
            )
            for r in rows
        ],
        **paging.metadata(total),
        # Echoed so the UI can render the active sort without duplicating the
        # default, and sortable so it never has to guess the accepted keys.
        sort=sorts[0]["key"],
        dir=sorts[0]["dir"],
        sorts=sorts,
        sortable=sorted(_POSTING_SORT_EXPRESSIONS),
    )


# Catalog order. `added_at` is the id: ids are handed out as postings are first
# seen, so the order is the same and the primary key serves it on 1.1M rows.
_CATALOG_SORT_EXPRESSIONS = {
    "added_at": "j.id",
    "date_posted": "j.date_posted",
    "company": "lower(j.company)",
    "title": "lower(j.title)",
    "source": "j.source",
}


class CatalogPosting(BaseModel):
    """One catalog posting, whether or not any check has run on it. `closed` is
    what the latest page verification shows: 'open', 'closed', or null when
    nothing has looked. `on_board` is set only when the read
    names a person, and is that person's materialized board."""

    job_id: int
    url: str
    company: str | None
    title: str | None
    source: str | None
    locations: list[str]
    date_posted: datetime.datetime | None
    added_at: datetime.datetime
    available: bool
    closed: str | None
    on_board: bool | None


class CatalogPostings(BaseModel):
    rows: list[CatalogPosting]
    has_more: bool
    offset: int
    total: int | None
    column_filters: list[column_filters_.ColumnFilter]
    filter_fields: list[column_filters_.FilterField]
    sorts: list[dict[str, str]]
    sortable: list[str]


@router.get("/jobs/catalog")
def list_catalog_jobs(
    q: str | None = None,
    sources: str | None = None,
    column_filters: str | None = Query(default=None, max_length=12000),
    board_user: int | None = None,
    on_board: bool | None = None,
    sort: str = "added_at",
    dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
    with_total: bool = False,
    user: AuthedUser = Depends(require_admin),
) -> CatalogPostings:
    """Every posting in the catalog, checked or not, under the board's typed
    column filters. `/jobs` is the verdict ledger's view and never shows a
    posting nothing has checked; this is the catalog's."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rules, extra, params = column_filters_.compile_filters(column_filters, catalog_only=True)
    params.update({"limit": limit + 1, "offset": offset, "board_user": board_user})
    if q:
        extra.append("AND (j.company ILIKE %(q)s OR j.title ILIKE %(q)s OR j.url ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    wanted_sources = params_.csv(sources)
    if wanted_sources:
        extra.append("AND j.source = ANY(%(sources)s)")
        params["sources"] = wanted_sources
    if on_board is not None:
        if board_user is None:
            raise refuse(422, "BOARD_USER_REQUIRED", "on_board needs board_user")
        extra.append(
            f"AND {'' if on_board else 'NOT '}EXISTS (SELECT 1 FROM board_visible bv "
            "WHERE bv.user_id = %(board_user)s AND bv.job_id = j.id)"
        )
    sorts = sorting.parse(sort, dir, _CATALOG_SORT_EXPRESSIONS, "added_at")
    where = "\n".join(extra)
    # job_comp is joined only for a pay filter. The planner removes an unused
    # LEFT JOIN but still keeps j.url, so the unfiltered count read the 256 MB
    # url index instead of the 23 MB company index: 0.72-0.83 s against
    # 0.23 s warm on 1.48M rows (production, 2026-11-01).
    tables = "jobs j" + (
        " LEFT JOIN job_comp pay ON pay.url = j.url" if column_filters_.reads_pay(rules) else ""
    )
    rows = db.query(
        f"""
        SELECT j.id AS job_id, j.url, j.company, j.title, j.source,
               COALESCE(j.locations, '{{}}') AS locations, j.date_posted,
               j.created_at AS added_at, {catalog.IS_AVAILABLE.format(job="j")} AS available,
               {verdict_reads.closed_verdict("j.url")} AS closed,
               CASE WHEN %(board_user)s::int IS NULL THEN NULL
                    ELSE EXISTS (SELECT 1 FROM board_visible bv
                                 WHERE bv.user_id = %(board_user)s AND bv.job_id = j.id) END
                 AS on_board
        FROM {tables}
        WHERE TRUE {where}
        ORDER BY {sorting.clause(sorts, _CATALOG_SORT_EXPRESSIONS, not_null={"added_at"})}, j.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    total = None
    if with_total:
        # Exact count, 0.23 s warm unfiltered on 1.48M rows (2026-11-01); opt-in for that reason.
        row = db.query_one(f"SELECT COUNT(*) AS c FROM {tables} WHERE TRUE {where}", params)
        total = row["c"] if row else 0
    return CatalogPostings(
        rows=[CatalogPosting(**r) for r in rows[:limit]],
        has_more=len(rows) > limit,
        offset=offset,
        total=total,
        column_filters=rules,
        filter_fields=column_filters_.fields(catalog_only=True),
        sorts=sorts,
        sortable=sorted(_CATALOG_SORT_EXPRESSIONS),
    )


class QueryResponses(BaseModel):
    """Every model call against one url, whole rows, oldest first. This is the
    drawer that shows what was asked and what came back, so it carries the
    prompt and the page text the list deliberately leaves out."""

    rows: list[QueryRecord]


@router.get("/jobs/responses")
def job_responses(url: str, user: AuthedUser = Depends(require_admin)) -> QueryResponses:
    rows = db.query(
        f"SELECT {_QUERY_DETAIL_COLUMNS}, instructions_id FROM ledger_rows WHERE url = %s ORDER BY id ASC",
        (url,),
    )
    # ponytail: one lookup per held row, as GET /queries/{id} does; batch it if
    # a posting's history grows past a drawer's worth of calls.
    for row in rows:
        if row["input_content"] is None:
            row["input_content"] = _held_input(row)
    return QueryResponses(
        rows=[QueryRecord.model_validate(row) for row in query_instructions.hydrate(rows)]
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
    """`content_failures` and `unfetchable` mean what they do on the list."""

    rows: list[TimelineEntry]
    content_failures: int
    unfetchable: bool


@router.get("/jobs/timeline")
def job_timeline(url: str, user: AuthedUser = Depends(require_admin)) -> PostingTimeline:
    failures = verdicts.fetch_failure_streaks([url]).get(url, 0)
    return PostingTimeline(
        content_failures=failures,
        unfetchable=failures >= int(db.get_config("fetch_give_up_after_failures")),
        rows=db.query_as(
            TimelineEntry,
            "SELECT id, created_at, config_name, check_type, status, reason, model, "
            "total_tokens, duration_ms, error "
            "FROM ledger_rows WHERE url = %s ORDER BY id ASC",
            (url,),
        ),
    )


@router.get("/jobs/path")
def job_path(
    url: str, board_user: int | None = None, user: AuthedUser = Depends(require_admin)
) -> posting_path.PostingPath:
    """Every published board's and every enabled filter's path for one posting,
    or, with `board_user`, that person's filters and personal board: the view
    they get, including why the posting is or is not on their board."""
    if board_user is not None:
        row = db.query_one("SELECT id FROM jobs WHERE url = %s", (url,))
        path = posting_path.for_user(row["id"], board_user) if row else None
    else:
        path = posting_path.for_admin(url)
    if path is None:
        raise refuse(404, "NOT_FOUND", "unknown job")
    return path
