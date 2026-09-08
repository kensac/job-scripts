"""The extension's side of assisted apply: the profile, the answer bank,
resolving a form, recording what was submitted, and the report that says
what to improve next. api.apply holds the resolving itself."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import ai, apply, budget, db
from api.auth import AuthedUser, require_user
from api.routers.jobs import _write_board_row
from api.tasks import application as drafts
from core.forms import posting_urls

router = APIRouter()

SUBMITTED_STATUS = "Application Submitted"
_BANK_COLS = "id, label, kind, value, times_used, last_used_at, updated_at"


def _bad(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


@router.get("/user/profile")
def get_profile(user: AuthedUser = Depends(require_user)):
    return apply.load_profile(user.id).model_dump()


@router.put("/user/profile")
def put_profile(body: apply.Profile, user: AuthedUser = Depends(require_user)):
    """The whole profile, replaced. A resume id that is not the person's is
    dropped rather than refused, so a deleted resume does not wedge the
    profile."""
    if body.default_resume_id is not None and not db.query_one(
        "SELECT 1 FROM user_resumes WHERE id = %s AND user_id = %s",
        (body.default_resume_id, user.id),
    ):
        body.default_resume_id = None
    db.execute(
        """
        INSERT INTO user_settings (user_id, profile, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (user_id) DO UPDATE SET profile = EXCLUDED.profile, updated_at = now()
        """,
        (user.id, db.jsonb(body.model_dump())),
    )
    return body.model_dump()


class AnswerPut(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


@router.get("/user/answers")
def list_answers(user: AuthedUser = Depends(require_user)):
    return {
        "answers": db.query(
            f"SELECT {_BANK_COLS} FROM application_answer_bank WHERE user_id = %s "
            "ORDER BY times_used DESC, updated_at DESC",
            (user.id,),
        )
    }


@router.put("/user/answers/{answer_id}")
def put_answer(answer_id: int, body: AnswerPut, user: AuthedUser = Depends(require_user)):
    row = db.query_one(
        f"UPDATE application_answer_bank SET value = %s, updated_at = now() "
        f"WHERE id = %s AND user_id = %s RETURNING {_BANK_COLS}",
        (body.value.strip(), answer_id, user.id),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "unknown answer")
    return row


@router.delete("/user/answers/{answer_id}")
def delete_answer(answer_id: int, user: AuthedUser = Depends(require_user)):
    if not db.query_one(
        "DELETE FROM application_answer_bank WHERE id = %s AND user_id = %s RETURNING id",
        (answer_id, user.id),
    ):
        raise _bad(404, "NOT_FOUND", "unknown answer")
    return {"ok": True}


class ResolveBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    fields: list[apply.Field_] = Field(max_length=300)
    # Which page of a multi-page form this is; the ledger keeps one fill per
    # page so a Workday application is several rows on one url.
    step: int = Field(default=0, ge=0, le=50)


@router.post("/user/apply/resolve")
def resolve_form(body: ResolveBody, user: AuthedUser = Depends(require_user)):
    """What goes in each field. Opens a fill in the ledger; the extension
    closes it with /submitted once the person has clicked submit."""
    job = db.query_one("SELECT id FROM jobs WHERE url = ANY(%s) LIMIT 1", (posting_urls(body.url),))
    job_id = job["id"] if job else None
    fields = apply.resolve(user.id, job_id, body.fields)
    fill = db.query_one(
        "INSERT INTO application_fills (user_id, job_id, url, host, fields) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (user.id, job_id, body.url, urlsplit(body.url).netloc, db.jsonb(fields)),
    )
    assert fill
    profile = apply.load_profile(user.id)
    resume = (
        db.query_one(
            "SELECT id, name, filename, pdf IS NOT NULL AS has_pdf FROM user_resumes "
            "WHERE id = %s AND user_id = %s",
            (profile.default_resume_id, user.id),
        )
        if profile.default_resume_id
        else None
    )
    return {
        "fill_id": fill["id"],
        "job_id": job_id,
        "fields": fields,
        "resume": resume,
        # The rows a repeated group (education, experience) is filled from;
        # a config-driven reader takes them one entry at a time.
        "profile": {
            "experience": [e.model_dump() for e in profile.experience],
            "education": [e.model_dump() for e in profile.education],
        },
    }


class SubmittedField(BaseModel):
    key: str = Field(max_length=300)
    final: str | None = Field(default=None, max_length=20000)
    remember: bool = False


class SubmittedBody(BaseModel):
    fields: list[SubmittedField] = Field(max_length=300)


@router.post("/user/apply/fills/{fill_id}/submitted")
def fill_submitted(fill_id: int, body: SubmittedBody, user: AuthedUser = Depends(require_user)):
    """The person clicked submit. Every field's final value goes on the
    ledger beside what was filled; a field they asked to remember goes in
    the bank; the board row flips to submitted."""
    fill = db.query_one(
        "SELECT id, job_id, fields FROM application_fills WHERE id = %s AND user_id = %s",
        (fill_id, user.id),
    )
    if not fill:
        raise _bad(404, "NOT_FOUND", "unknown fill")
    finals = {f.key: f for f in body.fields}
    fields = []
    for read in fill["fields"]:
        sub = finals.get(read["key"])
        final = sub.final if sub else read.get("value")
        entry = {**read, "final": final, "changed": (final or "") != (read.get("value") or "")}
        fields.append(entry)
        if sub and sub.remember and final and entry["kind"] != "file" and entry["label"]:
            db.execute(
                """
                INSERT INTO application_answer_bank (user_id, label, label_norm, kind, value)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, label_norm) DO UPDATE
                    SET value = EXCLUDED.value, kind = EXCLUDED.kind, updated_at = now()
                """,
                (user.id, entry["label"], apply.normalize(entry["label"]), entry["kind"], final),
            )
        elif entry["rung"] == "bank" and not entry["changed"]:
            db.execute(
                "UPDATE application_answer_bank SET times_used = times_used + 1, "
                "last_used_at = now() WHERE user_id = %s AND label_norm = %s",
                (user.id, apply.normalize(entry["label"])),
            )
    db.execute(
        "UPDATE application_fills SET fields = %s, submitted_at = now() WHERE id = %s",
        (db.jsonb(fields), fill_id),
    )
    if fill["job_id"] is not None:
        _write_board_row(user.id, fill["job_id"], {"status": SUBMITTED_STATUS})
    return {"ok": True, "job_id": fill["job_id"], "fields": fields}


class SuggestField(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    label: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="text", max_length=20)
    options: list[str] = Field(default_factory=list, max_length=200)
    # The profile's answer when the options did not recognisably hold it.
    hint: str | None = Field(default=None, max_length=200)


class SuggestBody(BaseModel):
    fields: list[SuggestField] = Field(min_length=1, max_length=100)
    job_id: int | None = None
    # The fill this belongs to; the model's answers go on its ledger row.
    fill_id: int | None = None


class SuggestedAnswer(BaseModel):
    key: str
    answer: str


class Suggestions(BaseModel):
    answers: list[SuggestedAnswer]


DEFAULT_SUGGEST = (
    "You fill the fields of a job application form that the person's profile "
    "did not fill by rule. Answer every field from the profile and resume; do "
    "not invent facts. For a field with options, the answer is exactly one "
    "of the options as written; when a hint is given, it is the person's own "
    "answer and you choose the option that means it. A text field takes a "
    "short phrase, a number field a number, and a long field a short paragraph "
    "in the person's own voice drawn from the resume. When the profile and resume "
    "do not say, answer with an empty string rather than a guess."
)


def never_filled(fields: list[SuggestField]) -> list[str]:
    """The keys of the fields the admin list keeps from the model."""
    raw = db.get_config("application_ai_never_fills") or ""
    words = [w.strip().lower() for w in re.split(r"[\n,]", raw) if w.strip()]
    if not words:
        return []
    return [
        f.key
        for f in fields
        if any(re.search(rf"\b{re.escape(w)}\b", f"{f.label} {f.key}".lower()) for w in words)
    ]


@router.post("/user/apply/suggest")
async def suggest(body: SuggestBody, user: AuthedUser = Depends(require_user)):
    """Everything the ladder left blank, in one live call on the person's
    own model settings. The extension fills the answers; the person still
    sees them in the form before submitting, and an answer left in place
    goes into the bank on submit."""
    skipped = never_filled(body.fields)
    fields = [f for f in body.fields if f.key not in skipped]
    if not fields:
        _note_on_fill(user.id, body.fill_id, {}, {}, skipped)
        return {"answers": {}, "skipped": skipped, "model": None}
    profile = apply.load_profile(user.id)
    resume = drafts.resume_text(user.id, profile.default_resume_id) or ""
    ent = budget.get_entitlement(user)
    try:
        cfg = budget.resolve_ai_config(user.id, ent)
    except PermissionError as exc:
        raise _bad(402, "BUDGET_EXCEEDED", "weekly budget spent") from exc
    except LookupError as exc:
        raise _bad(402, "NO_API_KEY", "no key to draft with") from exc
    job = (
        db.query_one("SELECT company, title FROM jobs WHERE id = %s", (body.job_id,))
        if body.job_id
        else None
    )
    parts = []
    if job:
        parts.append(f"Job: {job['title']} at {job['company']}")
    parts.append("Fields:\n" + json.dumps([f.model_dump() for f in fields], indent=1))
    if profile.notes:
        parts.append("The person's standing answers, in their own words:\n" + profile.notes)
    parts.append("Profile:\n" + profile.model_dump_json(exclude={"default_resume_id", "notes"}))
    if resume:
        parts.append("Resume:\n" + resume)
    rules = (db.get_config("application_suggest_instructions") or "").strip() or DEFAULT_SUGGEST
    parsed, usage = await ai.parse(cfg, rules, "\n\n".join(parts), Suggestions)
    budget.record_usage(
        user.id,
        cfg.key_source,
        drafts.PURPOSE,
        cfg.model,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("total_tokens", 0),
    )
    if parsed is None:
        raise _bad(502, "NO_ANSWER", "the model returned no usable answer; try again")
    by_key = {f.key: f for f in fields}
    answers = {}
    raw = {}
    for a in parsed.answers:
        field = by_key.get(a.key)
        text = a.answer.strip()
        if not field or not text:
            continue
        raw[a.key] = text
        if field.options:
            text = apply.pick_option(text, field.options) or ""
        if text:
            answers[a.key] = text
    _note_on_fill(user.id, body.fill_id, answers, raw, skipped)
    return {"answers": answers, "skipped": skipped, "model": cfg.model}


def _note_on_fill(
    user_id: int, fill_id: int | None, answers: dict, raw: dict, skipped: list[str]
) -> None:
    """The model's answers on the fill's ledger row as they return, so a
    fill nobody submitted still says what the model said: the text as
    written (ai_answer), the value it became (rung "ai") when an option
    recognisably held it, and the fields kept from it."""
    if fill_id is None:
        return
    fill = db.query_one(
        "SELECT fields FROM application_fills WHERE id = %s AND user_id = %s",
        (fill_id, user_id),
    )
    if not fill:
        return
    fields = []
    for read in fill["fields"]:
        entry = dict(read)
        key = entry.get("key")
        if key in raw:
            entry["ai_answer"] = raw[key]
        if key in answers:
            entry["rung"] = "ai"
            entry["value"] = answers[key]
        if key in skipped:
            entry["never_ai"] = True
        fields.append(entry)
    db.execute(
        "UPDATE application_fills SET fields = %s WHERE id = %s", (db.jsonb(fields), fill_id)
    )


class ReportBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    note: str = Field(default="", max_length=4000)
    page: dict[str, Any]


MAX_REPORT_BYTES = 2_000_000


@router.post("/user/apply/reports", status_code=201)
def create_report(body: ReportBody, user: AuthedUser = Depends(require_user)):
    """The extension's report button: whatever it saw, kept whole for
    triage. Capped so one page cannot fill the table by itself."""
    if len(json.dumps(body.page)) > MAX_REPORT_BYTES:
        raise _bad(413, "REPORT_TOO_LARGE", "the page capture is over 2 MB")
    return db.query_one(
        "INSERT INTO application_reports (user_id, url, host, note, page) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at",
        (user.id, body.url, urlsplit(body.url).netloc, body.note.strip(), db.jsonb(body.page)),
    )


@router.get("/user/apply/reports")
def list_reports(limit: int = 50, user: AuthedUser = Depends(require_user)):
    return {
        "reports": db.query(
            "SELECT id, url, host, note, created_at, page->>'title' AS title, "
            "jsonb_array_length(COALESCE(page->'fields', '[]'::jsonb)) AS fields "
            "FROM application_reports WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user.id, max(1, min(limit, 500))),
        )
    }


@router.get("/user/apply/reports/{report_id}")
def get_report(report_id: int, user: AuthedUser = Depends(require_user)):
    row = db.query_one(
        "SELECT id, url, host, note, page, created_at FROM application_reports "
        "WHERE id = %s AND user_id = %s",
        (report_id, user.id),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "unknown report")
    return row


@router.get("/user/apply/fills")
def list_fills(limit: int = 50, user: AuthedUser = Depends(require_user)):
    return {
        "fills": db.query(
            "SELECT f.id, f.job_id, f.url, f.host, f.created_at, f.submitted_at, "
            "j.company, j.title, jsonb_array_length(f.fields) AS fields "
            "FROM application_fills f LEFT JOIN jobs j ON j.id = f.job_id "
            "WHERE f.user_id = %s ORDER BY f.created_at DESC LIMIT %s",
            (user.id, max(1, min(limit, 500))),
        )
    }


@router.get("/user/apply/report")
def report(user: AuthedUser = Depends(require_user)) -> dict[str, Any]:
    """How the ladder is doing on this person's submitted forms: fields by
    rung, how many the person changed, and the labels most often left
    blank or corrected. The blank list is the backlog: each label on it
    is a rule to add or a fact the profile is missing."""
    rows = db.query(
        """
        SELECT f.host, e.value->>'label' AS label, e.value->>'kind' AS kind,
               COALESCE(e.value->>'rung', '') AS rung,
               COALESCE((e.value->>'changed')::boolean, false) AS changed
        FROM application_fills f, jsonb_array_elements(f.fields) e
        WHERE f.user_id = %s AND f.submitted_at IS NOT NULL
        """,
        (user.id,),
    )
    by_rung: dict[str, int] = {}
    blank: dict[str, int] = {}
    corrected: dict[str, int] = {}
    unchanged = 0
    for r in rows:
        by_rung[r["rung"] or "blank"] = by_rung.get(r["rung"] or "blank", 0) + 1
        norm = apply.normalize(r["label"] or "")
        if not r["rung"]:
            blank[norm] = blank.get(norm, 0) + 1
        elif r["changed"]:
            corrected[norm] = corrected.get(norm, 0) + 1
        else:
            unchanged += 1

    def top(counts: dict[str, int]) -> list[dict[str, Any]]:
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:25]
        return [{"label": k, "count": v} for k, v in ranked]

    submitted = db.query_one(
        "SELECT count(*) AS n FROM application_fills WHERE user_id = %s AND submitted_at IS NOT NULL",
        (user.id,),
    )
    return {
        "forms_submitted": (submitted or {}).get("n", 0),
        "fields": len(rows),
        "filled_unchanged": unchanged,
        "by_rung": by_rung,
        "most_often_blank": top(blank),
        "most_often_corrected": top(corrected),
    }
