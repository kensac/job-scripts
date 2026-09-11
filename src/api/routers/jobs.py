from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db, events, signals, sorting, task_admission
from api import params as params_
from api.ai import access as ai_access
from api.auth import AuthedUser, require_user
from api.board import visibility
from api.board.access import require_visible_job
from api.models import UploadRequest, UserJobPatch, UserJobsBulkIds, UserJobsBulkPatch
from api.problem import refuse
from core.fetching.urls import normalize_url

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
# The applicant tracking system a posting's url lives on, as the board
# filters and labels it. The host names the ATS for the hosted ones; the
# rest fold into "other", because on a real board the tail is wide (183
# employer careers hosts against five systems on 2026-09-07) and a select
# of 188 entries filters nothing. One expression, used by the row, the
# filter and the options.
ATS_SQL = """
    CASE
      WHEN j.url ILIKE 'https://jobs.ashbyhq.com/%%' THEN 'ashby'
      WHEN j.url ILIKE '%%greenhouse.io/%%' THEN 'greenhouse'
      WHEN j.url ILIKE 'https://jobs.lever.co/%%' THEN 'lever'
      WHEN j.url ILIKE '%%workable.com/%%' THEN 'workable'
      WHEN j.url ILIKE '%%myworkdayjobs.com/%%' THEN 'workday'
      WHEN j.url ILIKE '%%smartrecruiters.com/%%' THEN 'smartrecruiters'
      WHEN j.url ILIKE '%%icims.com/%%' THEN 'icims'
      WHEN j.url ILIKE '%%jobvite.com/%%' THEN 'jobvite'
      WHEN j.url ILIKE '%%bamboohr.com/%%' THEN 'bamboohr'
      WHEN j.url ILIKE '%%rippling.com/%%' THEN 'rippling'
      ELSE 'other'
    END
"""

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
_STATUS_META: dict[str, tuple[bool, str | None]] = {
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
    outcome: str | None


class AtsCount(BaseModel):
    ats: str
    count: int


class ReportKind(BaseModel):
    kind: str
    label: str


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
    ats: str
    url: str
    raw_url: str | None
    active: bool
    date_posted: datetime.datetime | None
    added_at: datetime.datetime
    extraction_status: str | None
    # float, not Decimal. psycopg hands back a Decimal and FastAPI's encoder
    # turned it into a number, which is what the board has always received and
    # what its type says. Declaring Decimal would make pydantic serialise it as
    # a STRING, silently, and the schema would agree with neither.
    comp_min: float | None
    comp_max: float | None
    comp_text: str | None
    comp_currency: str | None
    comp_period: str | None
    comp_basis: str | None
    closed_verdict: str | None
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
    comp_period: str | None
    comp_basis: str | None
    created_at: datetime.datetime
    closed_verdict: str | None


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
    status: str
    reason: str | None
    model: str | None
    created_at: datetime.datetime


class FilterVerdictRow(BaseModel):
    """One of this person's filters, and how it last judged this posting.
    Everything but the filter itself is null when it has never run on it."""

    name: str
    enabled: bool
    status: str | None
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


class Explained(BaseModel):
    """One check re-run on purpose, with the reasoning the cheap path skips.

    `refetched` and `closure_signal` are present only when the content could
    not be fetched and the fetch itself said why: a 404 or a removal notice is
    an answer about the posting, not a failure to get one."""

    check: str
    status: str
    reason: str | None
    refetched: bool | None = None
    closure_signal: str | None = None


class BulkDeleted(BaseModel):
    ok: bool
    deleted: int


class AcceptedUpload(BaseModel):
    job_id: int
    url: str


class RejectedUpload(BaseModel):
    url: str
    error: str


class Uploaded(BaseModel):
    """Accepted and rejected separately, with the reason on each rejection: an
    upload is the one place a person chooses the url, so being told now beats
    a job that silently never extracts."""

    accepted: list[AcceptedUpload]
    rejected: list[RejectedUpload]


class ReportFiled(BaseModel):
    id: int
    status: str
    created_at: datetime.datetime


class TaskState(BaseModel):
    """A task this person started. Ownership lives in the payload, so the
    kinds that stamp no user_id are fleet work with nobody to show them to."""

    id: int
    kind: str
    status: str
    progress: dict[str, Any] | None
    error: str | None
    created_at: datetime.datetime
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None


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


REPORT_KINDS = ("stale", "wrong_data", "closed", "other")
_REPORT_LABELS = {
    "stale": "Posting is stale",
    "wrong_data": "Details are wrong",
    "closed": "Posting is closed",
    "other": "Something else",
}


def report_kinds() -> list[ReportKind]:
    """The kinds a report can carry, with the label the form shows; one copy,
    served to the board's report modal and the admin reports page."""
    return [ReportKind(kind=k, label=_REPORT_LABELS[k]) for k in REPORT_KINDS]


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


def _touchable(user: AuthedUser, job_ids: list[int]) -> set[int]:
    """The ids this user may write a board row for.

    Pinning an unsubscribed job by patching it is a deliberate feature (the
    "watching" case). But a user_jobs row IS a visibility grant - visibility.FULL
    trusts a row the person acted on unconditionally - so an unrestricted pin
    launders around every other gate: pin, then read the job's cached page
    through /detail. The public catalog is fine to pin; another user's
    private upload is not, and that is the only distinction that matters.
    """
    rows = db.query(
        "SELECT id FROM jobs WHERE id = ANY(%s) AND (uploaded_by IS NULL OR uploaded_by = %s)",
        (job_ids, user.id),
    )
    return {r["id"] for r in rows}


def _write_board_row(user_id: int, job_id: int, patch: dict, *, publish: bool = True) -> dict:
    """Applies one patch to one board row; returns what it filled in itself.
    publish=False is for a bulk caller that will publish once for all its
    rows: the per-row publish is a synchronous post, and the bulk endpoint
    exists so a large selection is one request."""
    fields = dict(patch)
    autofilled = {}
    existing = None
    if "status" in fields or "date_applied" not in fields:
        existing = db.query_one(
            "SELECT status, date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
    # Setting any real status implies the user acted on the job; stamp
    # date_applied once so they never have to fill it by hand.
    if fields.get("status") and "date_applied" not in fields:
        if not existing or existing["date_applied"] is None:
            # UTC, not the container's local date. The containers run
            # TZ=America/New_York, so date.today() silently decided
            # "today" in Eastern for every user regardless of theirs.
            fields["date_applied"] = datetime.datetime.now(datetime.UTC).date()
            autofilled["date_applied"] = fields["date_applied"].isoformat()
    if "status" in fields:
        old_status = existing["status"] if existing else None
        if (old_status or "") != (fields["status"] or ""):
            db.execute(
                "INSERT INTO user_job_history (user_id, job_id, old_status, new_status) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, job_id, old_status, fields["status"]),
            )
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    insert_cols = ", ".join(fields)
    insert_vals = ", ".join(f"%({k})s" for k in fields)
    written = db.query_one(
        f"""
        INSERT INTO user_jobs (user_id, job_id, {insert_cols})
        VALUES (%(uid)s, %(jid)s, {insert_vals})
        ON CONFLICT (user_id, job_id) DO UPDATE SET {cols}, updated_at = now()
        RETURNING status, date_applied, hidden
        """,
        {"uid": user_id, "jid": job_id, **fields},
    )
    # Every path that writes a board row ends here, so this is the one place
    # an open board learns of the change without a reload.
    if publish:
        events.publish_board_row(user_id, job_id, written or fields)
    return autofilled


def _patch_fields(body: UserJobPatch) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    return fields


@router.patch("/user/jobs/{job_id}", response_model_exclude_none=True)
def patch_job(
    job_id: int, body: UserJobPatch, user: AuthedUser = Depends(require_user)
) -> PatchResult:
    if job_id not in _touchable(user, [job_id]):
        raise refuse(404, "NOT_FOUND", "unknown job")
    fields = _patch_fields(body)
    return PatchResult(ok=True, autofilled=Autofilled(**_write_board_row(user.id, job_id, fields)))


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
    allowed = _touchable(user, body.job_ids)
    changed = [j for j in body.job_ids if j in allowed]
    for job_id in changed:
        _write_board_row(user.id, job_id, fields, publish=False)
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


class ExplainBody(BaseModel):
    check: str


@router.post("/user/jobs/{job_id}/explain")
async def explain_check(
    job_id: int, body: ExplainBody, user: AuthedUser = Depends(require_user)
) -> Explained:
    """On-demand debugging: re-runs one check with the reason-ful schema and
    fuller reasoning (default verdicts skip reasons to save output tokens).
    Records a fresh verdict row (context 'explain') and returns the reason."""
    import dataclasses

    from api import budget
    from api.ai import verdicts as _verdicts
    from core.answers import FilterVerdict
    from core.checks import POSTING_CHECKS
    from core.filters import build_custom_instructions

    # This route writes a verdict into ai_queries, which has no user_id and is
    # resolved latest-row-per-(url, check_type) for EVERY user. An ungated
    # job_id here is therefore not a read leak but a write primitive against
    # everyone's board.
    job = require_visible_job(user, job_id, "j.id, j.url, j.company, j.title")
    fresh, closure_signal = await _verdicts.refresh_content(
        job["url"], company=job["company"], job_title=job["title"], context="explain"
    )
    if fresh is None:
        gone = db.query_one(
            "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
            "ORDER BY id DESC LIMIT 1",
            (job["url"],),
        )
        if closure_signal:
            return Explained(
                check=body.check,
                status="rejected",
                reason=(gone or {}).get("reason", ""),
                refetched=True,
                closure_signal=closure_signal,
            )
        raise HTTPException(
            409,
            detail={"code": "NO_CONTENT", "message": "could not fetch this posting just now"},
        )
    content_row = {"input_content": fresh}
    cfg = ai_access.require_config(user)
    cfg = dataclasses.replace(cfg, params={**cfg.params, "reasoning_effort": "medium"})

    check = body.check
    filter_name = prompt_hash = None
    spec = POSTING_CHECKS.get(check)
    if spec:
        instructions, model_cls, verdict_of = (
            spec.instructions,
            spec.response_model,
            spec.verdict_of,
        )
    elif check.startswith("filter:"):
        flt = db.query_one(
            "SELECT name, prompt, on_ambiguous, prompt_hash FROM user_filters "
            "WHERE user_id = %s AND id = %s",
            (user.id, int(check.split(":", 1)[1])),
        )
        if not flt:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
        instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
        model_cls = FilterVerdict
        verdict_of = lambda p: (p.should_filter, p.reason)
        filter_name = f"user{user.id}:{flt['name']}"
        prompt_hash = flt["prompt_hash"]
        check = "custom"
    else:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_CHECK",
                "message": f"check must be one of {', '.join(POSTING_CHECKS)}, or filter:<id>",
            },
        )

    with budget.record_parse_failures(user.id, cfg.key_source, "explain", cfg.model):
        parsed, usage = await _verdicts.run_check(
            cfg,
            url=job["url"],
            check_type=check,
            instructions=instructions,
            input_text=content_row["input_content"][:60000],
            response_model=model_cls,
            verdict_of=verdict_of,
            company=job["company"],
            job_title=job["title"],
            filter_name=filter_name,
            prompt_hash=prompt_hash,
            context="explain",
        )
    budget.record_tokens(
        user.id,
        cfg.key_source,
        "explain",
        cfg.model,
        usage,
    )
    if parsed is None:
        # run_check records the 'failed' row and returns None when the model
        # produces no parseable output; the tokens are already spent, so this
        # must read as a real outcome rather than an unhandled AttributeError.
        raise HTTPException(
            502,
            detail={
                "code": "NO_VERDICT",
                "message": "the model returned no usable answer; try again",
            },
        )
    rejected, reason = verdict_of(parsed)
    return Explained(check=body.check, status="rejected" if rejected else "passed", reason=reason)


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


@router.post("/uploads")
def upload_links(body: UploadRequest, user: AuthedUser = Depends(require_user)) -> Uploaded:
    from api import ssrf

    accepted: list[AcceptedUpload] = []
    rejected: list[RejectedUpload] = []
    for submitted in body.urls:
        raw = submitted.strip()
        if not raw.startswith(("http://", "https://")):
            continue
        # Fail here as well as in the fetcher: an upload is the one place a
        # user chooses the URL, and rejecting it now gives them an answer
        # instead of a job that silently never extracts.
        error = ssrf.validate_public_url(raw)
        if error:
            rejected.append(RejectedUpload(url=raw, error=error))
            continue
        url = normalize_url(raw)
        row = db.query_one(
            """
            INSERT INTO jobs (url, raw_url, source, uploaded_by, extraction_status)
            VALUES (%s, %s, 'upload', %s, 'pending')
            ON CONFLICT (url) DO UPDATE SET
                extraction_status = CASE WHEN jobs.extraction_status = 'failed'
                                         THEN 'pending' ELSE jobs.extraction_status END
            RETURNING id, extraction_status
            """,
            (url, raw, user.id),
        )
        assert row is not None
        db.execute(
            "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user.id, row["id"]),
        )
        if row["extraction_status"] == "pending":
            task_admission.enqueue("extract_upload", {"job_id": row["id"]}, {"user_id": user.id})
        accepted.append(AcceptedUpload(job_id=row["id"], url=url))
    return Uploaded(accepted=accepted, rejected=rejected)


class JobReport(BaseModel):
    kind: str
    message: str = ""
    corrections: dict | None = None


@router.post("/user/jobs/{job_id}/report")
def report_job(
    job_id: int, body: JobReport, user: AuthedUser = Depends(require_user)
) -> ReportFiled:
    if body.kind not in REPORT_KINDS:
        raise HTTPException(
            400,
            detail={"code": "INVALID_KIND", "message": f"kind must be one of {REPORT_KINDS}"},
        )
    if not db.query_one("SELECT id FROM jobs WHERE id = %s", (job_id,)):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    row = db.query_one_as(
        ReportFiled,
        """
        INSERT INTO reports (user_id, job_id, kind, message, corrections)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, status, created_at
        """,
        (
            user.id,
            job_id,
            body.kind,
            body.message[:2000],
            db.jsonb(body.corrections) if body.corrections is not None else None,
        ),
    )
    assert row is not None  # an insert with RETURNING always yields its row
    return row


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: AuthedUser = Depends(require_user)) -> TaskState:
    # Task ids are sequential and `error` is str(exc) written verbatim by the
    # worker, so an ungated lookup hands any signed-in user every other user's
    # failures. Ownership lives in the payload: every user-initiated kind
    # stamps user_id there, and the kinds that do not (ingest_source,
    # verify_new, data_health...) are fleet work with no user to show it to.
    row = db.query_one_as(
        TaskState,
        "SELECT id, kind, status, progress, error, created_at, started_at, finished_at "
        "FROM tasks WHERE id = %s AND (payload->>'user_id')::bigint = %s",
        (task_id, user.id),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown task"})
    return row
