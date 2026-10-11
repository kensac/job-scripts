"""Published request and response models for assisted apply."""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel

from api import apply


class RememberedAnswer(BaseModel):
    """One answer the bank kept from a submitted form."""

    id: int
    label: str
    kind: str
    value: str
    times_used: int
    last_used_at: datetime.datetime | None
    updated_at: datetime.datetime | None


class AnswerList(BaseModel):
    answers: list[RememberedAnswer]


class Fill(BaseModel):
    """One pass of the extension over one form. `job_id` is null when the url
    matched no posting on the board, which is normal rather than an error."""

    id: int
    job_id: int | None
    url: str
    host: str | None
    created_at: datetime.datetime
    submitted_at: datetime.datetime | None
    company: str | None
    title: str | None
    fields: int


class FillList(BaseModel):
    fills: list[Fill]


class ProblemReport(BaseModel):
    """One row of the problem-report list, declared rather than implied.

    The row shape used to reach the frontend as an untyped dict, so the types
    on the other side were written by hand from reading this query. They
    drifted: a capture field typed as a string was an object, and the page
    crashed on 2026-09-10 when a report with fields was opened. A declared
    shape puts that in openapi.json, which is where the frontend's types come
    from.
    """

    id: int
    url: str
    host: str | None
    note: str
    created_at: datetime.datetime
    title: str | None
    fields: int


class ReportList(BaseModel):
    reports: list[ProblemReport]


class ProblemReportDetail(BaseModel):
    """One report with the capture itself, which is the part triage reads.
    The list above carries a title and a field count out of the same column
    rather than the whole page."""

    id: int
    url: str
    host: str | None
    note: str
    page: dict[str, Any]
    created_at: datetime.datetime


class ReportCreated(BaseModel):
    """The extension shows the number, so the person can quote it."""

    id: int
    created_at: datetime.datetime


class MatchedPosting(BaseModel):
    """The posting the url matched, when the board holds one."""

    id: int
    company: str | None
    title: str | None
    status: str | None = None
    date_applied: datetime.date | None = None
    submitted_at: datetime.datetime | None = None


class ContextDraft(BaseModel):
    """A draft already written for this posting. Only drafts that exist are
    listed, so `draft` is never null here."""

    key: str
    question: str
    draft: str


class ApplyContext(BaseModel):
    """The three sources a fill draws from, before anything is filled.

    `profile` is a map rather than the profile shape: only the facts that are
    set are listed, because a blank fact fills nothing and listing it would
    say the profile has something it does not.
    """

    job: MatchedPosting | None
    profile: dict[str, Any]
    answers: list[RememberedAnswer]
    drafts: list[ContextDraft]


class ResumeFile(BaseModel):
    """The resume a fill would attach. `has_pdf` false means there is text to
    draft from and no file to upload."""

    id: int
    name: str
    filename: str | None
    has_pdf: bool


class ResolvedForm(BaseModel):
    """What goes in each field, and what the extension needs to put it there.

    `profile` is the whole profile as a map, minus the default resume and the
    notes: a config-driven reader fills a repeated group one row at a time and
    a selector that names another fact reads the scalar from it, so the keys
    are read at runtime rather than known here.
    """

    fill_id: int
    job_id: int | None
    fields: list[apply.ResolvedField]
    resume: ResumeFile | None
    profile: dict[str, Any]


class Submitted(BaseModel):
    """The ledger row after the person clicked submit.

    An entry stays a map on purpose. It is the field as read, plus the final
    value and whether it changed, plus whatever the model's pass left on it -
    so naming the keys here would silently drop the ones it did not name.
    """

    ok: bool = True
    job_id: int | None
    fields: list[dict[str, Any]]


class Suggested(BaseModel):
    """The model's pass over what the ladder left blank. `answers` is keyed by
    field key and holds only what came back usable; `skipped` is what the
    admin list keeps from the model, which is not the same as unanswered."""

    answers: dict[str, str]
    skipped: list[str]
    model: str | None


class LabelCount(BaseModel):
    label: str
    count: int


class ApplyReport(BaseModel):
    """How the ladder is doing on this person's submitted forms. `by_rung`
    is keyed by the rung that filled the field, with "blank" for the ones
    nothing filled."""

    forms_submitted: int
    fields: int
    filled_unchanged: int
    by_rung: dict[str, int]
    most_often_blank: list[LabelCount]
    most_often_corrected: list[LabelCount]
