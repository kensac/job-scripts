from __future__ import annotations

import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db, events, signals, sorting
from api import params as params_
from api.auth import AuthedUser, require_user
from api.board import visibility
from api.board.access import require_visible_job
from api.board.person_state import touchable_job_ids, write_board_row
from api.models import UserJobPatch, UserJobsBulkIds, UserJobsBulkPatch
from api.problem import refuse
from api.reports import ReportKind, report_kinds
from api.routers import job_explain, job_reports, job_tasks, job_uploads
from core.comp import CompBasis, CompPeriod

router = APIRouter()

# closed_verdict is three-valued and must stay that way: 'open', 'closed', or
# NULL for never checked. `active` cannot answer this question - it is whatever
# a board's feed last said and nothing ever clears it, so on a board that is
# not re-listed (sheet_import, imported once from a sheet) it reports our own
# stale copy as the posting's state. The closed check observes the posting
# itself and is applied uniformly across boards, which is what makes it
# comparable. Collapsing NULL into 'closed' would reintroduce exactly the bug
# this column exists to fix: 114 of the applications flagged dead by `active`
# have a closed-check that says the posting is open.
# The host names the ATS for the hosted ones; the rest fold into "other",
# because on a real board the tail is wide (183 employer careers hosts against
# five systems on 2026-09-07) and a select of 188 entries filters nothing.
AtsName = Literal[
    "ashby",
    "greenhouse",
    "lever",
    "workable",
    "workday",
    "smartrecruiters",
    "icims",
    "jobvite",
    "bamboohr",
    "rippling",
    "other",
]

# What a posting check answered. The queries that read one restrict to these
# two, so a third value cannot arrive without the SQL changing with it.
Verdict = Literal["passed", "rejected"]

# How a status ended, for a board that must not decide "is this over" or
# "which tone" by matching a name it hand-copied.
Outcome = Literal["won", "lost", "withdrawn"]

# Whether the posting is still up, as the closed check saw it. Three-valued and
# it must stay that way: null is "never checked", which is not "open".
Openness = Literal["open", "closed"]

# Where a posting's own extraction got to. Written only by the upload handler
# and the extractors.
ExtractionStatus = Literal["pending", "done", "failed"]
# The applicant tracking system a posting's url lives on, as one definition
# used by the row, the filter and the options. The patterns and the names are
# one table so a system added to the SQL cannot be missing from the type that
# declares what the column can hold, which is how a Literal turns into a 500.
_ATS_PATTERNS: dict[AtsName, str] = {
    "ashby": "https://jobs.ashbyhq.com/%%",
    "greenhouse": "%%greenhouse.io/%%",
    "lever": "https://jobs.lever.co/%%",
    "workable": "%%workable.com/%%",
    "workday": "%%myworkdayjobs.com/%%",
    "smartrecruiters": "%%smartrecruiters.com/%%",
    "icims": "%%icims.com/%%",
    "jobvite": "%%jobvite.com/%%",
    "bamboohr": "%%bamboohr.com/%%",
    "rippling": "%%rippling.com/%%",
}

ATS_SQL = (
    "\n    CASE\n"
    + "".join(
        f"      WHEN j.url ILIKE '{pattern}' THEN '{name}'\n"
        for name, pattern in _ATS_PATTERNS.items()
    )
    + "      ELSE 'other'\n    END\n"
)

_JOB_ROW = f"""
    j.id AS job_id, j.company, j.title, j.locations, j.terms, j.source,
    ({ATS_SQL}) AS ats,
    j.url, j.raw_url, j.active, j.date_posted, j.created_at AS added_at,
    j.extraction_status, j.comp_min, j.comp_max, j.comp_text, j.comp_currency,
    j.comp_period, j.comp_basis,
    (SELECT CASE q.status WHEN 'passed' THEN 'open' WHEN 'rejected' THEN 'closed' END
     FROM ai_queries q
     WHERE q.url = j.url AND q.check_type = 'closed' AND q.status IN ('passed', 'rejected')
     ORDER BY q.id DESC LIMIT 1) AS closed_verdict,
    uj.status, uj.date_applied, uj.notes, uj.size, uj.recruiter,
    uj.connection1, uj.connection2, uj.documents,
    COALESCE(uj.hidden, FALSE) AS hidden
"""


# Whitelisted server-side sort columns (all NULLS LAST so empty cells sink).
_SORTABLE = {
    "id": "j.id",
    "added_at": "j.created_at",
    "date_posted": "j.date_posted",
    "date_applied": "uj.date_applied",
    "company": "lower(j.company)",
    "title": "lower(j.title)",
    "source": "j.source",
    "status": "uj.status",
    "comp": "j.comp_max",
}

NOT_APPLIED = "not_applied"

# Canonical status vocabulary, backend-owned. The column stays free text (the
# sheet import brought arbitrary values), so options are served as this canon
# unioned with whatever statuses actually exist on the user's rows.
DEFAULT_STATUSES = [
    "Application Submitted",
    "Follow-up",
    "Recruiter Screen",
    "Online Assessment",
    "Interview",
    "Final Round",
    "Offer",
    "Accepted",
    "Rejected",
    "No Longer Interested",
]

# What a status MEANS, served beside the names so the board never decides
# "is this over" or "which tone" by matching a name it hand-copied: a status
# absent here is in play with no outcome. The withdrawn pair mirrors
# mail_pipeline.WITHDRAWN_STATUSES.
_STATUS_META: dict[str, tuple[bool, Outcome | None]] = {
    "Accepted": (True, "won"),
    "Rejected": (True, "lost"),
    "No Longer Interested": (True, "withdrawn"),
    "Withdrawn": (True, "withdrawn"),
}


class StatusMeta(BaseModel):
    """What a status MEANS, served beside the names so the board never decides
    "is this over" or "which tone" by matching a name it hand-copied. A status
    absent from the table is in play with no outcome."""

    name: str
    terminal: bool
    outcome: Outcome | None


class AtsCount(BaseModel):
    ats: AtsName
    count: int


class BoardOptions(BaseModel):
    """Everything the board's filter and edit controls need, generated from
    data instead of hardcoded in the client.

    `ats` here is the board-wide count, the profile of the board as a whole.
    The per-page counts under the other filters are `facets` on the list."""

    statuses: list[str]
    status_meta: list[StatusMeta]
    not_applied_sentinel: str
    sources: list[str]
    ats: list[AtsCount]
    report_kinds: list[ReportKind]


class BoardRow(BaseModel):
    """`_JOB_ROW`, in types: the posting, the ATS read off its url, and this
    person's own row over the top.

    `closed_verdict` is three-valued and must stay that way: 'open', 'closed',
    or null for never checked. `active` cannot answer this question, it is
    whatever a board's feed last said and nothing ever clears it."""

    job_id: int
    company: str | None
    title: str | None
    locations: list[str]
    terms: list[str]
    source: str
    ats: AtsName
    url: str
    raw_url: str | None
    active: bool
    date_posted: datetime.datetime | None
    added_at: datetime.datetime
    extraction_status: ExtractionStatus | None
    # float, not Decimal. psycopg hands back a Decimal and FastAPI's encoder
    # turned it into a number, which is what the board has always received and
    # what its type says. Declaring Decimal would make pydantic serialise it as
    # a STRING, silently, and the schema would agree with neither.
    comp_min: float | None
    comp_max: float | None
    comp_text: str | None
    comp_currency: str | None
    # Narrowed on measurement, not on the vocabulary alone: every comp_period
    # and comp_basis in production is one of these, so the type is the real
    # value set rather than a hope. `status` below is NOT narrowed, and that is
    # the same rule going the other way: it is free text by design.
    comp_period: CompPeriod | None
    comp_basis: CompBasis | None
    closed_verdict: Openness | None
    status: str | None
    date_applied: datetime.date | None
    notes: str | None
    size: str | None
    recruiter: str | None
    connection1: str | None
    connection2: str | None
    documents: str | None
    hidden: bool
    # A window count rides on the page when the caller asked for a total, so
    # one board read answers both. Excluded from the response: it is the same
    # number on every row and it is reported once, as `total`.
    total_rows: int | None = Field(default=None, exclude=True)


class Sort(BaseModel):
    key: str
    dir: str


class Facets(BaseModel):
    """Counts under every OTHER filter the page has on. Taken before the ats
    clause joins the rest, because a board-wide "rippling 4" beside a lens
    that holds none of them reads as a lie."""

    ats: list[AtsCount]


class Board(BaseModel):
    """One page of the board, and everything the page needs to render its own
    controls without re-deriving them."""

    rows: list[BoardRow]
    # Only the filters that narrowed anything, echoed back.
    filters: dict[str, list[str]]
    next_cursor: int | None
    has_more: bool
    offset: int
    total: int | None
    facets: Facets | None
    # The active sort as applied, so the UI renders it without duplicating the
    # default, and the keys it may ask for.
    sorts: list[Sort]
    sortable: list[str]
    # When the board's membership was last computed, so a page can say "as of"
    # and a person knows a preference change has landed.
    board_computed_at: datetime.datetime | None


class JobFacts(BaseModel):
    """The posting behind one board row. Wider than `BoardRow` where the
    detail view needs it and narrower where the list does."""

    id: int
    url: str
    raw_url: str | None
    company: str | None
    title: str | None
    locations: list[str]
    terms: list[str]
    source: str
    active: bool
    date_posted: datetime.datetime | None
    comp_min: float | None
    comp_max: float | None
    comp_text: str | None
    comp_currency: str | None
    comp_period: CompPeriod | None
    comp_basis: CompBasis | None
    created_at: datetime.datetime
    closed_verdict: Openness | None


class OwnRow(BaseModel):
    """This person's own row, which is null when they have never touched the
    posting. A board row is a grant as well as a record, so absence is a real
    answer rather than an empty one."""

    status: str | None
    date_applied: datetime.date | None
    notes: str | None
    size: str | None
    recruiter: str | None
    connection1: str | None
    connection2: str | None
    documents: str | None
    hidden: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime


class StatusChange(BaseModel):
    old_status: str | None
    new_status: str | None
    created_at: datetime.datetime


class CheckVerdict(BaseModel):
    """The latest verdict for one posting check."""

    check_type: str
    status: Verdict
    reason: str | None
    model: str | None
    created_at: datetime.datetime


class FilterVerdictRow(BaseModel):
    """One of this person's filters, and how it last judged this posting.
    Everything but the filter itself is null when it has never run on it."""

    name: str
    enabled: bool
    status: Verdict | None
    reason: str | None
    model: str | None
    created_at: datetime.datetime | None


class JobDetail(BaseModel):
    """Everything behind one board row: the posting, the content that was
    judged, this person's own row and its history, and why the checks and
    filters let it through."""

    job: JobFacts
    signals: signals.Signals
    row: OwnRow | None
    history: list[StatusChange]
    content: str | None
    content_fetched_at: datetime.datetime | None
    checks: list[CheckVerdict]
    filter_verdicts: list[FilterVerdictRow]


class BulkDeleted(BaseModel):
    ok: bool
    deleted: int


class Autofilled(BaseModel):
    """What the write filled in that the caller did not send.

    A status change can date the row by itself, so the caller is told rather
    than having to re-read the row to find out.

    An absent key is the answer, not a null: the route sets
    response_model_exclude_none so `{}` still means "nothing was filled",
    which is what the board already reads. Declaring the shape must not move
    the wire.
    """

    status: str | None = None
    date_applied: datetime.date | None = None


class PatchResult(BaseModel):
    ok: bool
    autofilled: Autofilled


class BulkPatchResult(BaseModel):
    """`skipped` names the ids the caller may not touch rather than refusing
    the whole selection: a selection made from the board can include a row
    that vanished or was never theirs, and failing everything for one would
    send the page back to a request per row.
    """

    ok: bool
    updated: int
    skipped: list[int]


class Deleted(BaseModel):
    ok: bool


def status_meta(statuses: list[str]) -> list[StatusMeta]:
    return [
        StatusMeta(name=name, terminal=meta[0], outcome=meta[1])
        for name in statuses
        for meta in (_STATUS_META.get(name, (False, None)),)
    ]


@router.get("/user/jobs/options")
def job_options(user: AuthedUser = Depends(require_user)) -> BoardOptions:
    """Everything the board's filter/edit controls need, generated from data
    instead of hardcoded in the client."""
    in_use = [
        r["status"]
        for r in db.query(
            "SELECT DISTINCT status FROM user_jobs "
            "WHERE user_id = %s AND status IS NOT NULL AND status != '' ORDER BY status",
            (user.id,),
        )
    ]
    statuses = DEFAULT_STATUSES + [s for s in in_use if s not in DEFAULT_STATUSES]
    sources = [
        r["source"]
        for r in db.query(
            "SELECT source FROM user_sources WHERE user_id = %s ORDER BY source",
            (user.id,),
        )
    ]
    # The ATSs on this person's board with how many rows each, most first,
    # so the filter offers what is there rather than a fixed list.
    ats = db.query_as(
        AtsCount,
        visibility.FAST.format(
            columns=f"({ATS_SQL}) AS ats, COUNT(*) AS count",
            extra="AND COALESCE(uj.hidden, FALSE) = FALSE GROUP BY 1 ORDER BY 2 DESC, 1",
        ),
        {"uid": user.id},
    )
    return BoardOptions(
        statuses=statuses,
        status_meta=status_meta(statuses),
        not_applied_sentinel=NOT_APPLIED,
        sources=sources,
        ats=ats,
        report_kinds=report_kinds(),
    )


@router.get("/user/jobs")
def list_jobs(
    limit: int = 200,
    offset: int = 0,
    cursor: int | None = None,
    sort: str = "added_at",
    dir: str = "desc",
    search: str | None = None,
    status: str | None = None,
    statuses: str | None = None,
    source: str | None = None,
    sources: str | None = None,
    ats: str | None = None,
    include_hidden: bool = False,
    with_total: bool = False,
    with_facets: bool = False,
    user: AuthedUser = Depends(require_user),
) -> Board:
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    sorts = (
        [{"key": "id", "dir": "desc"}]
        if cursor is not None
        else sorting.parse(sort, dir, _SORTABLE, "added_at")
    )
    extra = []
    params: dict = {"uid": user.id, "limit": limit + 1, "offset": offset}
    if not include_hidden:
        extra.append("AND COALESCE(uj.hidden, FALSE) = FALSE")
    if search:
        extra.append(
            "AND (j.company ILIKE %(search)s OR j.title ILIKE %(search)s OR j.url ILIKE %(search)s)"
        )
        params["search"] = f"%{search}%"
    wanted = params_.csv_with_scalar(statuses, status)
    wanted_sources = params_.csv_with_scalar(sources, source)
    wanted_ats = list(dict.fromkeys(value.lower() for value in params_.csv(ats)))
    if wanted:
        named = [s for s in wanted if s != NOT_APPLIED]
        clauses = []
        if named:
            clauses.append("uj.status = ANY(%(statuses)s)")
            params["statuses"] = named
        if NOT_APPLIED in wanted:
            clauses.append("(uj.status IS NULL OR uj.status = '')")
        extra.append(f"AND ({' OR '.join(clauses)})")
    if wanted_sources:
        extra.append("AND j.source = ANY(%(sources)s)")
        params["sources"] = wanted_sources
    # The ATS counts a select should show are the counts under every OTHER
    # filter the page has on: a board-wide "rippling 4" beside a lens that
    # holds none of them reads as a lie (2026-09-07). So the facet is taken
    # before the ats clause joins the rest.
    facets = None
    if with_facets:
        facets = Facets(
            ats=db.query_as(
                AtsCount,
                visibility.FAST.format(
                    columns=f"({ATS_SQL}) AS ats, COUNT(*) AS count",
                    extra="\n".join(extra) + "\nGROUP BY 1 ORDER BY 2 DESC, 1",
                ),
                params,
            )
        )
    if wanted_ats:
        extra.append(f"AND ({ATS_SQL}) = ANY(%(ats)s)")
        params["ats"] = wanted_ats

    filter_sql = "\n".join(extra)
    total = None
    if cursor is not None:
        # Legacy cursor mode: fixed newest-first by id.
        order = "AND j.id < %(cursor)s\nORDER BY j.id DESC LIMIT %(limit)s"
        params["cursor"] = cursor
    else:
        order = (
            f"ORDER BY {sorting.clause(sorts, _SORTABLE)}, j.id DESC "
            "LIMIT %(limit)s OFFSET %(offset)s"
        )
    # One pass, not two: the total rides on the page as a window count over
    # the same filtered set, so a sort with with_total costs one board read
    # rather than the count query and then the page query.
    count_on_page = with_total and cursor is None
    columns = _JOB_ROW + (", COUNT(*) OVER () AS total_rows" if count_on_page else "")
    sql = visibility.FAST.format(columns=columns, extra=f"{filter_sql}\n{order}")
    rows = db.query_as(BoardRow, sql, params)
    if with_total:
        if count_on_page and rows:
            total = rows[0].total_rows
        else:
            # Cursor position is pagination, not a filter. Count the full
            # selection in cursor mode and when an offset page has no rows.
            row = db.query_one(
                visibility.FAST.format(columns="COUNT(*) AS c", extra=filter_sql), params
            )
            total = row["c"] if row else 0
    has_more = len(rows) > limit
    rows = rows[:limit]
    return Board(
        rows=rows,
        filters=params_.applied(status=wanted, source=wanted_sources, ats=wanted_ats),
        next_cursor=rows[-1].job_id if cursor is not None and has_more and rows else None,
        has_more=has_more,
        offset=offset,
        total=total,
        facets=facets,
        sorts=[Sort(**s) for s in sorts],
        sortable=sorted(_SORTABLE),
        board_computed_at=visibility.computed_at(user.id),
    )


def _patch_fields(body: UserJobPatch) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    return fields


@router.patch("/user/jobs/{job_id}", response_model_exclude_none=True)
def patch_job(
    job_id: int, body: UserJobPatch, user: AuthedUser = Depends(require_user)
) -> PatchResult:
    if job_id not in touchable_job_ids(user.id, [job_id]):
        raise refuse(404, "NOT_FOUND", "unknown job")
    fields = _patch_fields(body)
    return PatchResult(ok=True, autofilled=Autofilled(**write_board_row(user.id, job_id, fields)))


@router.patch("/user/jobs")
def patch_jobs(
    body: UserJobsBulkPatch, user: AuthedUser = Depends(require_user)
) -> BulkPatchResult:
    """One patch across a selection, so a 6,000-row selection is one request
    rather than 6,000. Ids the caller may not touch are skipped and named,
    not refused whole: a selection made from the board can include a row that
    vanished or was never theirs, and failing everything for it would send
    the page back to one request per row."""
    fields = _patch_fields(body.patch)
    allowed = touchable_job_ids(user.id, body.job_ids)
    changed = [j for j in body.job_ids if j in allowed]
    for job_id in changed:
        write_board_row(user.id, job_id, fields, publish=False)
    # One event for the whole selection, off the per-row path.
    events.publish_board_rows(user.id, changed, fields)
    updated = len(changed)
    return BulkPatchResult(
        ok=True,
        updated=updated,
        skipped=[j for j in body.job_ids if j not in allowed],
    )


@router.get("/user/jobs/{job_id}/detail")
def job_detail(job_id: int, user: AuthedUser = Depends(require_user)) -> JobDetail:
    """Everything behind one board row: cached posting content, the user's own
    row + status history, and why the AI let it through (per-filter verdicts
    plus the closed/clearance checks)."""
    job = require_visible_job(
        user,
        job_id,
        "j.id, j.url, j.raw_url, j.company, j.title, j.locations, j.terms, j.source, "
        "j.active, j.date_posted, j.comp_min, j.comp_max, j.comp_text, j.comp_currency, "
        "j.comp_period, j.comp_basis, "
        "j.created_at, "
        "(SELECT CASE q.status WHEN 'passed' THEN 'open' WHEN 'rejected' THEN 'closed' END "
        " FROM ai_queries q WHERE q.url = j.url AND q.check_type = 'closed' "
        " AND q.status IN ('passed', 'rejected') ORDER BY q.id DESC LIMIT 1) AS closed_verdict",
    )
    content_row = db.query_one(
        "SELECT input_content, created_at FROM ai_queries "
        "WHERE url = %s AND check_type = 'content' AND input_content IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (job["url"],),
    )
    checks = db.query_as(
        CheckVerdict,
        """
        SELECT DISTINCT ON (check_type) check_type, status, reason, model, created_at
        FROM ai_queries
        WHERE url = %(url)s AND check_type IN ('closed', 'clearance')
          AND status IN ('passed', 'rejected')
        ORDER BY check_type, id DESC
        """,
        {"url": job["url"]},
    )
    filter_verdicts = db.query_as(
        FilterVerdictRow,
        """
        SELECT f.name, f.enabled, v.status, v.reason, v.model, v.created_at
        FROM user_filters f
        LEFT JOIN LATERAL (
            SELECT status, reason, model, created_at FROM ai_queries q
            WHERE q.url = %(url)s AND q.check_type = 'custom'
              AND q.prompt_hash = f.prompt_hash
              AND q.status IN ('passed', 'rejected')
            ORDER BY q.id DESC LIMIT 1
        ) v ON TRUE
        WHERE f.user_id = %(uid)s ORDER BY f.id
        """,
        {"url": job["url"], "uid": user.id},
    )
    return JobDetail(
        job=JobFacts(**job),
        # Every signal is optional and absence means it does not exist, never a
        # zero and never something for the caller to re-derive.
        signals=signals.signals_for(job),
        row=db.query_one_as(
            OwnRow,
            "SELECT status, date_applied, notes, size, recruiter, connection1, "
            "connection2, documents, hidden, created_at, updated_at "
            "FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user.id, job_id),
        ),
        history=db.query_as(
            StatusChange,
            "SELECT old_status, new_status, created_at FROM user_job_history "
            "WHERE user_id = %s AND job_id = %s ORDER BY id",
            (user.id, job_id),
        ),
        content=(content_row or {}).get("input_content"),
        content_fetched_at=(content_row or {}).get("created_at"),
        checks=checks,
        filter_verdicts=filter_verdicts,
    )


router.include_router(job_explain.router)


@router.delete("/user/jobs/{job_id}")
def delete_user_job(job_id: int, user: AuthedUser = Depends(require_user)) -> Deleted:
    """Drops the user's board row only (the catalog job is untouched); a later
    run re-materializes it if it still passes their filters. Hide is the
    permanent alternative."""
    db.execute("DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s", (user.id, job_id))
    return Deleted(ok=True)


@router.delete("/user/jobs")
def delete_user_jobs(
    body: UserJobsBulkIds, user: AuthedUser = Depends(require_user)
) -> BulkDeleted:
    """The selection form of the delete above: the caller's own rows only,
    one statement, count returned."""
    deleted = db.execute_count(
        "DELETE FROM user_jobs WHERE user_id = %s AND job_id = ANY(%s)", (user.id, body.job_ids)
    )
    return BulkDeleted(ok=True, deleted=deleted)


router.include_router(job_uploads.router)
router.include_router(job_reports.router)
router.include_router(job_tasks.router)
