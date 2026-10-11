"""Person pipeline summary, list, and application detail views."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db
from api import params as params_
from api.auth import AuthedUser, require_user
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.mail.current import current_match
from api.mail.pipeline import ApplicationEvent
from api.problem import refuse
from api.routers.mail.pipeline_rows import (
    ApplicationRow,
    PipelineApplication,
    application_rows_for_user,
    correction_actor_label,
)
from api.routers.mail.shared import Evidence, _evidence_for

router = APIRouter()

# A date that stands in for "no date" while sorting. It never leaves this
# module and never reaches a response - the boolean beside it is what actually
# orders undated rows, and this only stops the comparison raising on None.
_SORT_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)

_STAGE_SORT_RANK = {
    name: index
    for index, name in enumerate(
        (
            "applied",
            "acknowledged",
            "assessment",
            "interviewing",
            "offer",
            "rejected",
            "closed",
            "withdrawn",
        )
    )
}

_PIPELINE_SORT_KEYS: dict[str, Any] = {
    "applied_at": lambda r: (r.applied_at is None, r.applied_at or _SORT_EPOCH, r.id),
    "last_event_at": lambda r: (r.last_event_at is None, r.last_event_at or _SORT_EPOCH, r.id),
    "company": lambda r: ((r.company_name or "").lower(), r.id),
    "title": lambda r: ((r.title or "").lower(), r.id),
    "stage": lambda r: (_STAGE_SORT_RANK.get(r.stage, 99), r.id),
    "event_count": lambda r: (r.event_count, r.id),
}


class PipelineStage(BaseModel):
    """The stage vocabulary in process order, with which stages are over, so
    the lenses are read from here rather than copied into the client."""

    key: str
    label: str
    terminal: bool


class PipelineSummary(BaseModel):
    """Counts, not a funnel: terminal stages beat progress regardless of
    arrival order and a stage is recomputed rather than advanced, so nothing
    flows through anything.

    Dismissals are counted rather than hidden. They change every other number
    here, and a total that silently shrinks week to week with nothing
    explaining why is the failure this whole system keeps producing."""

    counts: dict[str, int]
    total: int
    with_evidence: int
    without_evidence: int
    dismissed: int
    stages: list[PipelineStage]


@router.get("/user/pipeline/summary")
def pipeline_summary(user: AuthedUser = Depends(require_user)) -> PipelineSummary:
    """Stage counts, derived by the same code that derives the list.

    An endpoint rather than a client-side sum: the moment the browser
    aggregates, the page owns a derivation the server owns, and the two drift
    the first time either changes. Counts, not a funnel - terminal stages beat
    progress regardless of arrival order and a stage is recomputed rather than
    advanced, so nothing flows through anything.
    """
    rows = application_rows_for_user(user.id)
    live = [r for r in rows if r.dismissed_at is None]
    counts: dict[str, int] = {}
    for row in live:
        counts[row.stage] = counts.get(row.stage, 0) + 1
    with_evidence = sum(1 for r in live if r.event_count)
    return PipelineSummary(
        counts=counts,
        total=len(live),
        with_evidence=with_evidence,
        without_evidence=len(live) - with_evidence,
        dismissed=len(rows) - len(live),
        stages=[
            PipelineStage(key=key, label=key.capitalize(), terminal=key in mail_pipeline.TERMINAL)
            for key in _STAGE_SORT_RANK
        ],
    )


class ApplicationAction(BaseModel):
    """An open ask, and what could ever close it without a person.

    `settles_on` is `mail_pipeline.settles_on`, not a second reading of it: an
    empty list means nothing that arrives can close this item, which is a
    different thing from an item still waiting, and such an item must not be
    rendered as a live obligation."""

    id: int
    user_id: int
    application_id: int | None
    event_id: int | None
    kind: str
    due_at: datetime.datetime | None
    resolved_at: datetime.datetime | None
    resolution: str | None
    resolved_by_event_id: int | None
    created_at: datetime.datetime
    settles_on: list[str]


class PipelineAction(ApplicationAction):
    """The same item on the list, which names the application it is about
    because the list has no application in hand."""

    company_name: str | None
    title: str | None


class Pipeline(BaseModel):
    applications: list[PipelineApplication]
    total: int
    has_more: bool
    filters: dict[str, list[str]]
    actions: list[PipelineAction]


@router.get("/user/pipeline")
def pipeline(
    include_closed: bool = Query(default=False),
    stage: str | None = Query(default=None),
    provenance: str | None = Query(default=None),
    tier: str | None = Query(default=None),
    evidence: bool | None = Query(default=None),
    silent_days: int | None = Query(default=None, ge=0),
    sort: str = Query(default="applied_at"),
    dir: str = Query(default="desc"),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
) -> Pipeline:
    """The user's applications with their derived stage and open actions.

    Stage is derived, so it cannot be filtered or paged in SQL. Both happen
    after derivation, which is why the bulk events query matters: the
    alternative is deriving a page's worth and having no idea what the totals
    are.
    """
    rows = application_rows_for_user(user.id)
    # Comma-separated, because every lens worth having is multi-valued:
    # "waiting" is applied AND acknowledged, "over" is rejected AND closed AND
    # withdrawn. A single-valued filter makes a lens either impossible or a
    # client-side re-derivation of a set the server already knows.
    stages = set(params_.csv(stage))
    if stage == "dismissed":
        rows = [r for r in rows if r.dismissed_at is not None]
        stages = set()
    else:
        rows = [r for r in rows if r.dismissed_at is None]
    if not include_closed and not stages:
        rows = [r for r in rows if r.stage not in mail_pipeline.TERMINAL]
    if stages:
        rows = [r for r in rows if r.stage in stages]
    provenances, tiers = params_.csv(provenance), params_.csv(tier)
    if provenances:
        rows = [r for r in rows if r.source_provenance in provenances]
    if tiers:
        rows = [r for r in rows if r.strongest_tier in tiers]
    if silent_days is not None:
        # Applied, and nothing since. `evidence=false` is "no mail at all";
        # this is "no mail LATELY", which is the ghosting question and the one
        # a person actually asks. Measured against the last thing that
        # happened - a reply, or applying if nothing has - because silence
        # since an acknowledgement is still silence.
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=silent_days)
        rows = [
            r
            for r in rows
            if (last := r.last_event_at or r.applied_at) is not None
            and last < cutoff
            and r.stage not in mail_pipeline.TERMINAL
        ]
    if evidence is not None:
        # The summary reports with_evidence and without_evidence; without this
        # the numbers are not clickable and the population behind them is
        # unreachable.
        rows = [r for r in rows if bool(r.event_count) is evidence]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in (r.company_name or "").lower() or needle in (r.title or "").lower()
        ]
    # Sorted on the SET, not the page. A client sorting what it was handed
    # sorts one page of a filtered whole and reads as though it sorted
    # everything - which is a quiet lie rather than a missing feature.
    #
    # Stage sorts by how far through the process it is rather than
    # alphabetically, because "applied, acknowledged, interviewing" is the
    # order the word means and "acknowledged, applied, assessment" is not.
    key = _PIPELINE_SORT_KEYS.get(sort, _PIPELINE_SORT_KEYS["applied_at"])
    rows.sort(key=key, reverse=dir != "asc")
    page = rows[offset : offset + limit]
    names = {
        r["id"]: r
        for r in db.query(
            "SELECT id, company_name, title FROM applications WHERE user_id = %s", (user.id,)
        )
    }
    actions = [
        {
            **item.model_dump(),
            "company_name": names[item.application_id]["company_name"],
            "title": names[item.application_id]["title"],
        }
        for item in mail_pipeline.action_items(user.id, open_only=True)
    ]
    return Pipeline(
        applications=page,
        total=len(rows),
        has_more=offset + len(page) < len(rows),
        filters=params_.applied(provenance=provenances, tier=tiers),
        actions=[PipelineAction(**action) for action in actions],
    )


class MatchRow(BaseModel):
    """One match attempt on this application, with the message it was about.

    `in_force` is marked rather than left to position: latest-wins is per
    MESSAGE, so the row in force is not simply the last one in the list."""

    id: int
    message_id: int
    application_id: int | None
    method: str
    confidence: str | None
    rationale: str | None
    created_at: datetime.datetime
    actor_user_id: int | None
    subject: str | None
    from_email: str | None
    sent_at: datetime.datetime | None
    in_force: bool


class PipelineMatch(MatchRow):
    """The attempt, what it was decided FROM, and who decided it.

    Without `evidence` a person has to open their mail client to check a
    match, which is the same as not being able to check it. `corrected_by` is
    derived rather than stored: the same actor id reads as "you" to the owner
    and as an administrator to anyone else, so there is no second copy of that
    distinction to drift."""

    evidence: Evidence | None
    corrected_by: str


class PipelineDetail(ApplicationRow):
    """One application with everything that decided it.

    The stored row, the stage derived from the events, and the events and
    matches themselves. It carries the row's own fields rather than nesting
    them, which is what the list row does too, so a client holding either
    reads `company_name` the same way."""

    stage: str
    events: list[ApplicationEvent]
    matches: list[PipelineMatch]
    actions: list[ApplicationAction]


@router.get("/user/pipeline/{application_id}")
def pipeline_detail(
    application_id: int, user: AuthedUser = Depends(require_user)
) -> PipelineDetail:
    """One application, its events, the messages behind them, and its actions.

    Events come oldest-first with `in_force` marked explicitly rather than left
    to position: latest-wins is per MESSAGE, so the row in force is not simply
    the last one in the list.
    """
    app = db.query_one_as(
        ApplicationRow,
        """
        SELECT a.id, a.job_id, a.company_name, a.title, a.applied_at,
               a.source_provenance, a.dismissed_at, a.dismissed_reason,
               uj.status AS board_status
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        WHERE a.id = %s AND a.user_id = %s
        """,
        (application_id, user.id),
    )
    if app is None:
        raise refuse(404, "NOT_FOUND", "application not found")
    events = mail_pipeline.events_for(application_id)
    matches = db.query_as(
        MatchRow,
        f"""
            WITH touched AS (
                SELECT DISTINCT message_id FROM application_matches WHERE application_id = %(app)s
            ),
            current_match AS (
                {current_match("id")}
            )
            SELECT am.id, am.message_id, am.application_id, am.method, am.confidence,
                   am.rationale, am.created_at, am.actor_user_id,
                   m.subject, m.from_email, m.sent_at,
                   (cm.id = am.id) AS in_force
            FROM application_matches am
            JOIN touched tm ON tm.message_id = am.message_id
            JOIN email_messages m ON m.id = am.message_id
            JOIN current_match cm ON cm.message_id = am.message_id
            ORDER BY am.message_id, am.id
            """,
        {"app": application_id},
    )
    # Every match carries what it was decided FROM, not just what it decided.
    # Without this a person has to open their mail client to check a match,
    # which is the same as not being able to check it.
    evidence = _evidence_for(sorted({m.message_id for m in matches}))
    # Both additions kept: #227 wraps actions in their settling state, this
    # branch adds who corrected each match. Independent answers to the same
    # question - what does this row let a person question.
    actions = [item.model_dump() for item in mail_pipeline.action_items(user.id, application_id)]
    return PipelineDetail(
        **app.model_dump(),
        stage=mail_pipeline.stage_for(events, app.board_status),
        events=events,
        matches=[
            PipelineMatch(
                **m.model_dump(),
                evidence=evidence.get(m.message_id),
                corrected_by=correction_actor_label(
                    m.actor_user_id,
                    user.id,
                    # A match has no `model` column. The matcher's own rows
                    # carry its tier as the method; only a person writes
                    # 'manual' or 'detached'.
                    model=None if m.method in mail_match.HUMAN_METHODS else m.method,
                ),
            )
            for m in matches
        ],
        actions=[ApplicationAction(**action) for action in actions],
    )
