"""The read model behind one board row."""

from __future__ import annotations

import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, signals
from api.auth import AuthedUser, require_user
from api.board.access import require_visible_job
from core.comp import CompBasis, CompPeriod

router = APIRouter()
Verdict = Literal["passed", "rejected"]
Openness = Literal["open", "closed"]


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
