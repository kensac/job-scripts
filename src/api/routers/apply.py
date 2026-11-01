"""The extension's side of assisted apply: the profile, the answer bank,
resolving a form, recording what was submitted, and the report that says
what to improve next. api.apply holds the resolving itself."""

from __future__ import annotations

import datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import (
    apply,
    db,
    events,
)
from api.apply import fill_answers, submissions
from api.auth import AuthedUser, require_user
from api.board.person_state import board_row, touchable_job_ids
from api.board.person_state import write_board_row as _write_board_row
from api.mail import applications
from api.problem import refuse
from api.routers.apply_routes import extension, profile, report_uploads, reports, suggestions
from api.routers.apply_routes.events import record_apply_event
from api.routers.apply_routes.models import ResolvedForm, ResumeFile, Submitted
from core.fetching import forms
from core.fetching.forms import posting_urls

router = APIRouter()

SUBMITTED_STATUS = "Application Submitted"


class TrackedFill(BaseModel):
    job_id: int
    url: str


@router.post("/user/apply/fills/{fill_id}/track")
def track_fill(fill_id: int, user: AuthedUser = Depends(require_user)) -> TrackedFill:
    from api import job_imports, ssrf
    from core.fetching import ats
    from core.fetching.urls import normalize_url

    fill = db.query_one(
        "SELECT url FROM application_fills WHERE id = %s AND user_id = %s",
        (fill_id, user.id),
    )
    if not fill:
        raise refuse(404, "NOT_FOUND", "unknown fill")
    url = ats.canonicalize(fill["url"]) or normalize_url(fill["url"])
    error = ssrf.validate_public_url(url)
    if error:
        raise refuse(422, "INVALID_URL", error)
    with db.transaction():
        locked = db.query_one(
            "SELECT job_id, submitted_at, superseded_by FROM application_fills "
            "WHERE id = %s AND user_id = %s FOR UPDATE",
            (fill_id, user.id),
        )
        if not locked:
            raise refuse(404, "NOT_FOUND", "unknown fill")
        if locked["submitted_at"] is not None or locked["superseded_by"] is not None:
            raise refuse(409, "FILL_SUBMITTED", "This application was already submitted.")
        if locked["job_id"] is not None:
            existing = db.query_one("SELECT url FROM jobs WHERE id = %s", (locked["job_id"],))
            if existing:
                url = existing["url"]
        saved = job_imports.save_posting(user.id, url, fill["url"])
        db.execute(
            "UPDATE application_fills SET job_id = %s WHERE id = %s", (saved["job_id"], fill_id)
        )
    job_imports.publish_saved(user.id, saved)
    return TrackedFill(job_id=saved["job_id"], url=saved["url"])


class ResolveBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    fields: list[apply.Field_] = Field(max_length=300)
    # Which page of a multi-page form this is; the ledger keeps one fill per
    # page so a Workday application is several rows on one url.
    step: int = Field(default=0, ge=0, le=50)
    # An open fill to add these fields to: a form reveals fields as it is
    # filled (the EEO race question appears once Hispanic/Latino is
    # answered), and they belong on the same ledger row.
    fill_id: int | None = None
    resume_open: bool = False


@router.post("/user/apply/resolve")
def resolve_form(body: ResolveBody, user: AuthedUser = Depends(require_user)) -> ResolvedForm:
    """What goes in each field. Opens a fill in the ledger; the extension
    closes it with /submitted once the person has clicked submit."""
    # The same match apply_context makes: another person's private upload at
    # this url is not this person's posting, and a fill attached to it would
    # carry its id into the drafts lookup and, on submit, a board row.
    job = db.query_one(
        "SELECT id FROM jobs WHERE url = ANY(%s) "
        "AND id NOT IN (SELECT job_id FROM posting_uploads WHERE uploaded_by <> %s) LIMIT 1",
        (posting_urls(body.url), user.id),
    )
    job_id = job["id"] if job else None
    fields = apply.resolve(user.id, job_id, body.fields)
    with db.transaction():
        open_fill = (
            db.query_one(
                "SELECT id, fields FROM application_fills WHERE id = %s AND user_id = %s "
                "AND submitted_at IS NULL AND superseded_by IS NULL AND url = %s FOR UPDATE",
                (body.fill_id, user.id, body.url),
            )
            if body.fill_id is not None
            else None
        )
        if open_fill is None and body.fill_id is None and body.resume_open and fields:
            open_fill = db.query_one(
                "SELECT id, fields FROM application_fills WHERE user_id = %s AND url = %s "
                "AND job_id IS NOT DISTINCT FROM %s AND submitted_at IS NULL "
                "AND fields @> %s::jsonb ORDER BY id DESC LIMIT 1 FOR UPDATE",
                (
                    user.id,
                    body.url,
                    job_id,
                    db.jsonb([{"key": f.key, "label": f.label, "kind": f.kind} for f in fields]),
                ),
            )
        if open_fill:
            have = {f["key"] for f in open_fill["fields"]}
            merged = [*open_fill["fields"], *[f.model_dump() for f in fields if f.key not in have]]
            db.execute(
                "UPDATE application_fills SET fields = %s WHERE id = %s",
                (db.jsonb(merged), open_fill["id"]),
            )
            fill = open_fill
        else:
            fill = db.query_one(
                "INSERT INTO application_fills (user_id, job_id, url, host, fields) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    user.id,
                    job_id,
                    body.url,
                    urlsplit(body.url).netloc,
                    db.jsonb([f.model_dump() for f in fields]),
                ),
            )
    assert fill
    profile = apply.load_profile(user.id)
    resume = (
        db.query_one_as(
            ResumeFile,
            "SELECT id, name, filename, "
            "(pdf_upload_id IS NOT NULL) AS has_pdf FROM user_resumes "
            "WHERE id = %s AND user_id = %s",
            (profile.default_resume_id, user.id),
        )
        if profile.default_resume_id
        else None
    )
    record_apply_event(
        "apply_form_resolved",
        user,
        host=urlsplit(body.url).netloc,
        matched_job=job_id is not None,
        fields=len(fields),
        filled=sum(1 for f in fields if f.value not in (None, "")),
        reused_fill=open_fill is not None,
        has_resume=resume is not None,
    )
    return ResolvedForm(
        fill_id=fill["id"],
        job_id=job_id,
        fields=fields,
        resume=resume,
        profile=profile.model_dump(exclude={"default_resume_id", "notes"}),
    )


class SubmittedField(BaseModel):
    key: str = Field(max_length=300)
    final: str | None = Field(default=None, max_length=20000)
    remember: bool = False


class SubmittedBody(BaseModel):
    fields: list[SubmittedField] = Field(max_length=300)


@router.post("/user/apply/fills/{fill_id}/submitted")
def fill_submitted(
    fill_id: int, body: SubmittedBody, user: AuthedUser = Depends(require_user)
) -> Submitted:
    """The person clicked submit. Every field's final value goes on the
    ledger beside what was filled; a field they asked to remember goes in
    the bank; the board row flips to submitted."""
    lock = (
        "SELECT id, job_id, url, fields, submitted_at, superseded_by FROM application_fills "
        "WHERE id = %s AND user_id = %s FOR UPDATE"
    )
    with db.transaction():
        fill = db.query_one(lock, (fill_id, user.id))
        if not fill:
            raise refuse(404, "NOT_FOUND", "unknown fill")
        # An empty fill left by a form the extension lost track of submits
        # onto the earlier fill that holds the fields.
        fill_id = submissions.target(user.id, fill)
        if fill_id != fill["id"]:
            fill = db.query_one(lock, (fill_id, user.id))
            assert fill is not None
        if fill["submitted_at"] is not None:
            return Submitted(job_id=fill["job_id"], fields=fill["fields"])
        finals = {f.key: f for f in body.fields}
        names = submissions.resume_names(user.id, fill["fields"])
        fields = []
        for read in fill["fields"]:
            sub = finals.get(read["key"])
            final = sub.final if sub else read.get("value")
            entry = {**read, "final": final}
            entry["changed"] = submissions.changed(entry, names)
            fields.append(entry)
            if sub and sub.remember and final and entry["kind"] != "file" and entry["label"]:
                db.execute(
                    """
                    INSERT INTO application_answer_bank (user_id, label, label_norm, kind, value)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, label_norm) DO UPDATE
                        SET value = EXCLUDED.value, kind = EXCLUDED.kind, updated_at = now()
                    """,
                    (
                        user.id,
                        entry["label"],
                        apply.normalize(entry["label"]),
                        entry["kind"],
                        final,
                    ),
                )
            elif entry["rung"] == "bank" and not entry["changed"]:
                db.execute(
                    "UPDATE application_answer_bank SET times_used = times_used + 1, "
                    "last_used_at = now() WHERE user_id = %s AND label_norm = %s",
                    (user.id, apply.normalize(entry["label"])),
                )
        submitted = db.query_one(
            "UPDATE application_fills SET fields = %s, submitted_at = now() WHERE id = %s "
            "RETURNING submitted_at",
            (db.jsonb(fields), fill_id),
        )
        assert submitted is not None
        # A fill opened before resolve_form matched only touchable postings
        # may still name another person's private upload. The board row is a
        # visibility grant, so it obeys the rule every other board write does.
        job_id = fill["job_id"]
        if job_id is not None and job_id not in touchable_job_ids(user.id, [job_id]):
            job_id = None
        if job_id is not None:
            # The board write records the application with the status.
            _write_board_row(user.id, job_id, {"status": SUBMITTED_STATUS}, publish=False)
            application_id = applications.from_board(user.id, job_id, None, set_date=False)
        else:
            application_id = applications.from_form(
                user.id, forms.board_of(fill["url"]), submitted["submitted_at"]
            )
        db.execute(
            "UPDATE application_fills SET application_id = %s WHERE id = %s",
            (application_id, fill_id),
        )
    if job_id is not None:
        events.publish_board_row(user.id, job_id, board_row(user.id, job_id))
    record_apply_event(
        "apply_form_submitted",
        user,
        matched_job=fill["job_id"] is not None,
        fields=len(fields),
        changed=sum(1 for f in fields if f["changed"]),
    )
    return Submitted(job_id=fill["job_id"], fields=fields)


class FillAnswerEdit(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    revision: int = Field(ge=0)
    value: str = Field(max_length=20000)
    feedback: str = Field(default="", max_length=4000)


class FillAnswerState(BaseModel):
    id: int
    job_id: int | None
    submitted_at: datetime.datetime | None
    fields: list[dict[str, Any]]


@router.get("/user/apply/fills/{fill_id}")
def get_fill_answers(fill_id: int, user: AuthedUser = Depends(require_user)) -> FillAnswerState:
    return FillAnswerState(**fill_answers.read(user.id, fill_id))


@router.put("/user/apply/fills/{fill_id}/answer")
def edit_fill_answer(
    fill_id: int, body: FillAnswerEdit, user: AuthedUser = Depends(require_user)
) -> FillAnswerState:
    fill_answers.edit(user.id, fill_id, body.key, body.revision, body.value, body.feedback)
    record_apply_event("apply_answer_edited", user, has_feedback=bool(body.feedback))
    return FillAnswerState(**fill_answers.read(user.id, fill_id))


router.include_router(extension.router)
router.include_router(profile.router)
router.include_router(suggestions.router)
router.include_router(reports.router)
router.include_router(report_uploads.router)
