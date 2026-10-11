"""Admin posting rollups, call responses, and timeline inspection."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, pagination, posting_path, sorting
from api import params as params_
from api.ai import verdicts
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin.query_ledger import _QUERY_DETAIL_COLUMNS, QueryRecord
from api.routers.admin.review_gates import ReviewDecisions
from api.routers.admin.shared import require_admin
from core import query_instructions

router = APIRouter()


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


_RECENT_POSTINGS = f"""
    WITH selected AS MATERIALIZED (
        SELECT q.url, q.created_at
        FROM ledger_rows q
        WHERE q.url IS NOT NULL AND q.id = (
            SELECT n.id FROM ledger_rows n WHERE n.url = q.url
            ORDER BY n.created_at DESC, n.id DESC LIMIT 1
        )
        ORDER BY q.created_at DESC, q.url
        LIMIT %(limit)s OFFSET %(offset)s
    )
    SELECT selected.url, totals.*, streak.failures AS content_failures
    FROM selected
    CROSS JOIN LATERAL (
        SELECT {_POSTING_TOTALS} FROM ledger_rows WHERE url = selected.url
    ) totals
    CROSS JOIN LATERAL ({verdicts.FETCH_STREAK.format(url="selected.url")}) streak
    ORDER BY selected.created_at DESC, selected.url
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
    if len(sub) == 1 and not having and sorts == [{"key": "last_seen", "dir": "desc"}]:
        # Walk the time index to find the page, then read history only for
        # those URLs. created_at is NOT NULL, so DESC has the same ordering
        # as DESC NULLS LAST and can use the existing index backwards.
        # The id tiebreaker selects one row when a URL has equal timestamps;
        # every check still contributes to that URL's reported totals.
        total_row = db.query_one(
            "SELECT COUNT(DISTINCT url) AS c FROM ledger_rows WHERE url IS NOT NULL"
        )
        rows = db.query(_RECENT_POSTINGS, page_params)
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


class QueryResponses(BaseModel):
    """Every model call against one url, whole rows, oldest first. This is the
    drawer that shows what was asked and what came back, so it carries the
    prompt and the page text the list deliberately leaves out."""

    rows: list[QueryRecord]


@router.get("/jobs/responses")
def job_responses(url: str, user: AuthedUser = Depends(require_admin)) -> QueryResponses:
    return QueryResponses(
        rows=[
            QueryRecord.model_validate(row)
            for row in query_instructions.hydrate(
                db.query(
                    f"SELECT {_QUERY_DETAIL_COLUMNS}, instructions_id FROM ledger_rows WHERE url = %s ORDER BY id ASC",
                    (url,),
                )
            )
        ]
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
    decisions: ReviewDecisions
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
        decisions=ReviewDecisions(),
    )


@router.get("/jobs/path")
def job_path(url: str, user: AuthedUser = Depends(require_admin)) -> posting_path.PostingPath:
    """Every published board's and every enabled filter's path for one posting."""
    path = posting_path.for_admin(url)
    if path is None:
        raise refuse(404, "NOT_FOUND", "unknown job")
    return path
