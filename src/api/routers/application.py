"""A person's resumes, and the answers to one job's application form.

The form's questions come from the ATS where it can be read and from the
person where it cannot; the drafts come from a batched task; the back-and-
forth on one draft is a live call, because a person is waiting for it. The
writing style lives on user settings beside the other preferences.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import io
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import ai, ai_access, budget, db, task_admission
from api.auth import AuthedUser, require_user
from api.job_access import require_visible_job
from core import forms
from core.store import get_content
from tasks import application as drafts

router = APIRouter()

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_RESUME_CHARS = 60_000
_RESUME_COLS = (
    "id, name, filename, length(text) AS chars, text, pdf IS NOT NULL AS has_pdf, "
    "created_at, updated_at"
)
_ANSWER_COLS = "key, question, source, required, draft, turns, model, updated_at"


class ResumeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    text: str | None = Field(default=None, max_length=MAX_RESUME_CHARS)
    pdf_base64: str | None = None
    filename: str | None = Field(default=None, max_length=200)


class ResumePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    text: str | None = Field(default=None, min_length=1, max_length=MAX_RESUME_CHARS)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def _bad(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


@router.get("/user/resumes")
def list_resumes(user: AuthedUser = Depends(require_user)):
    return {
        "resumes": db.query(
            f"SELECT {_RESUME_COLS} FROM user_resumes WHERE user_id = %s ORDER BY updated_at DESC",
            (user.id,),
        )
    }


@router.post("/user/resumes", status_code=201)
def create_resume(body: ResumeCreate, user: AuthedUser = Depends(require_user)):
    """Pasted text or a PDF, base64 in the body. The PDF's text is what is
    kept; the file itself is not stored. Same name replaces the text."""
    text = (body.text or "").strip()
    data = None
    if body.pdf_base64:
        try:
            data = base64.b64decode(body.pdf_base64, validate=True)
        except Exception as exc:
            raise _bad(400, "INVALID_PDF", "the attachment is not valid base64") from exc
        if len(data) > MAX_PDF_BYTES:
            raise _bad(413, "PDF_TOO_LARGE", "PDFs up to 5 MB")
        try:
            text = pdf_text(data)
        except Exception as exc:
            raise _bad(400, "INVALID_PDF", "could not read that PDF") from exc
        if not text:
            raise _bad(400, "NO_TEXT", "that PDF has no extractable text; paste the resume instead")
    if not text:
        raise _bad(400, "NO_TEXT", "paste the resume text or attach a PDF")
    # The PDF's bytes live in the database and are dumped and archived with
    # it, so the count per person is bounded as well as the size per file.
    kept = db.query_one(
        "SELECT count(*) AS n FROM user_resumes WHERE user_id = %s AND name != %s",
        (user.id, body.name.strip()),
    )
    cap = int(db.get_config("resumes_per_user", 10))
    if kept and kept["n"] >= cap:
        raise _bad(409, "TOO_MANY_RESUMES", f"up to {cap} resumes; delete one first")
    row = db.query_one(
        f"""
        INSERT INTO user_resumes (user_id, name, text, filename, pdf)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id, name) DO UPDATE
            SET text = EXCLUDED.text, filename = EXCLUDED.filename, pdf = EXCLUDED.pdf,
                updated_at = now()
        RETURNING {_RESUME_COLS}
        """,
        (user.id, body.name.strip(), text[:MAX_RESUME_CHARS], body.filename, data),
    )
    return row


@router.get("/user/resumes/{resume_id}/pdf")
def resume_pdf(resume_id: int, user: AuthedUser = Depends(require_user)):
    """The file as uploaded, for the extension to attach to a form. A pasted
    resume has no file."""
    from fastapi.responses import Response

    row = db.query_one(
        "SELECT filename, pdf FROM user_resumes WHERE id = %s AND user_id = %s",
        (resume_id, user.id),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "unknown resume")
    if row["pdf"] is None:
        raise _bad(404, "NO_FILE", "this resume was pasted; upload the PDF to attach it")
    name = (row["filename"] or "resume.pdf").replace('"', "")
    return Response(
        bytes(row["pdf"]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@router.patch("/user/resumes/{resume_id}")
def patch_resume(resume_id: int, body: ResumePatch, user: AuthedUser = Depends(require_user)):
    row = db.query_one(
        f"""
        UPDATE user_resumes
           SET name = COALESCE(%s, name), text = COALESCE(%s, text), updated_at = now()
         WHERE id = %s AND user_id = %s
        RETURNING {_RESUME_COLS}
        """,
        (body.name.strip() if body.name else None, body.text, resume_id, user.id),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "unknown resume")
    return row


@router.delete("/user/resumes/{resume_id}")
def delete_resume(resume_id: int, user: AuthedUser = Depends(require_user)):
    row = db.query_one(
        "DELETE FROM user_resumes WHERE id = %s AND user_id = %s RETURNING id",
        (resume_id, user.id),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "unknown resume")
    return {"ok": True}


def _job(user: AuthedUser, job_id: int) -> dict[str, Any]:
    return require_visible_job(user, job_id, "j.id, j.url, j.company, j.title")


def _answers(user_id: int, job_id: int) -> list[dict[str, Any]]:
    return db.query(
        f"SELECT {_ANSWER_COLS} FROM application_answers "
        "WHERE user_id = %s AND job_id = %s ORDER BY id",
        (user_id, job_id),
    )


@router.get("/user/jobs/{job_id}/application")
def get_application(job_id: int, user: AuthedUser = Depends(require_user)):
    """The form as read (or why it could not be), every question with its
    draft and its back-and-forth, and what the drafts would be written from."""
    job = _job(user, job_id)
    form = db.query_one(
        "SELECT questions, error, fetched_at FROM application_forms WHERE url = %s",
        (job["url"],),
    )
    answers = {a["key"]: a for a in _answers(user.id, job_id)}
    # Questions the form carries that this person has no row for yet, so the
    # page shows the form before the first draft is asked for.
    for q in (form or {}).get("questions") or []:
        answers.setdefault(
            q["key"],
            {
                "key": q["key"],
                "question": q["label"],
                "source": "form",
                "required": bool(q.get("required")),
                "draft": None,
                "turns": [],
                "model": None,
                "updated_at": None,
            },
        )
    # Whether the box wants prose. A one-line box (a URL, a salary) is a
    # field to fill, not an answer to draft; the view renders it as one and
    # the task drafts it only when asked for by key. A pasted question is
    # prose by definition.
    multiline = {q["key"]: q.get("kind") == "long" for q in (form or {}).get("questions") or []}
    for a in answers.values():
        a["multiline"] = multiline.get(a["key"], True)
    return {
        "job": job,
        "form": {
            "supported": forms.supported(job["url"]),
            "host": forms.host_of(job["url"]),
            "fetched_at": (form or {}).get("fetched_at"),
            "error": (form or {}).get("error"),
            "questions": len((form or {}).get("questions") or []),
        },
        "questions": list(answers.values()),
        "task": task_admission.in_flight(
            "application_draft", {"user_id": user.id, "job_id": job_id}
        ),
        "resumes": db.query(
            "SELECT id, name FROM user_resumes WHERE user_id = %s ORDER BY updated_at DESC",
            (user.id,),
        ),
        "writing_style": drafts.writing_style(user.id),
        "default_style": drafts.DEFAULT_STYLE,
    }


class QuestionAdd(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    required: bool = False


@router.post("/user/jobs/{job_id}/application/questions", status_code=201)
def add_question(job_id: int, body: QuestionAdd, user: AuthedUser = Depends(require_user)):
    """A question pasted in from a form this code cannot read. Keyed by its
    text, so pasting the same question twice is one row."""
    _job(user, job_id)
    text = " ".join(body.question.split())
    key = "m" + hashlib.sha1(text.lower().encode()).hexdigest()[:10]  # noqa: S324 - a key, not a secret
    row = db.query_one(
        f"""
        INSERT INTO application_answers (user_id, job_id, key, question, source, required)
        VALUES (%s, %s, %s, %s, 'manual', %s)
        ON CONFLICT (user_id, job_id, key) DO UPDATE SET required = EXCLUDED.required
        RETURNING {_ANSWER_COLS}
        """,
        (user.id, job_id, key, text, body.required),
    )
    return row


@router.delete("/user/jobs/{job_id}/application/questions/{key}")
def delete_question(job_id: int, key: str, user: AuthedUser = Depends(require_user)):
    _job(user, job_id)
    row = db.query_one(
        "DELETE FROM application_answers WHERE user_id = %s AND job_id = %s AND key = %s "
        "AND source = 'manual' RETURNING id",
        (user.id, job_id, key),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "no pasted question with that key")
    return {"ok": True}


class DraftRequest(BaseModel):
    resume_id: int | None = None
    keys: list[str] | None = Field(default=None, max_length=100)
    refresh: bool = False


@router.post("/user/jobs/{job_id}/application/draft", status_code=202)
def request_drafts(job_id: int, body: DraftRequest, user: AuthedUser = Depends(require_user)):
    """Queues the drafts. The task reads the form if it has not been read,
    then writes one draft per question, batched at half price where the
    person's key allows it."""
    _job(user, job_id)
    if body.resume_id is not None:
        if not db.query_one(
            "SELECT 1 FROM user_resumes WHERE id = %s AND user_id = %s",
            (body.resume_id, user.id),
        ):
            raise _bad(404, "NOT_FOUND", "unknown resume")
    elif not db.query_one("SELECT 1 FROM user_resumes WHERE user_id = %s", (user.id,)):
        raise _bad(400, "NO_RESUME", "add a resume under settings first")
    ai_access.require_config(user)
    admission = task_admission.enqueue(
        "application_draft",
        {"user_id": user.id, "job_id": job_id},
        {"resume_id": body.resume_id, "keys": body.keys, "refresh": body.refresh},
    )
    if admission.conflict:
        raise HTTPException(
            409,
            detail={
                "code": "IN_PROGRESS",
                "message": "drafts for this job are already being written",
                "task_id": admission.conflict["id"],
            },
        )
    return {"task_id": admission.task_id}


class AnswerPut(BaseModel):
    draft: str = Field(max_length=20_000)


@router.put("/user/jobs/{job_id}/application/answers/{key}")
def put_answer(job_id: int, key: str, body: AnswerPut, user: AuthedUser = Depends(require_user)):
    """The person's own edit. Kept as the draft and as a turn, so a later
    refinement starts from what they wrote rather than what the model did.
    An empty draft clears it; the page saves whatever the box holds on blur."""
    _job(user, job_id)
    turn = {"role": "user", "kind": "edit", "text": body.draft, "at": _now()}
    row = db.query_one(
        f"""
        UPDATE application_answers
           SET draft = NULLIF(%s, ''), turns = turns || %s::jsonb, updated_at = now(),
               draft_revision = draft_revision + 1
         WHERE user_id = %s AND job_id = %s AND key = %s
        RETURNING {_ANSWER_COLS}
        """,
        (body.draft, json.dumps([turn]), user.id, job_id, key),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "unknown question")
    return row


class RefineBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    resume_id: int | None = None


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


@router.post("/user/jobs/{job_id}/application/answers/{key}/refine")
async def refine_answer(
    job_id: int, key: str, body: RefineBody, user: AuthedUser = Depends(require_user)
):
    """One turn of back-and-forth on one draft: the person says what to
    change, the model rewrites from the current draft and the earlier
    requests. Live, on the person's own configured model, like explain."""
    job = _job(user, job_id)
    row = db.query_one(
        f"SELECT {_ANSWER_COLS} FROM application_answers "
        "WHERE user_id = %s AND job_id = %s AND key = %s",
        (user.id, job_id, key),
    )
    if not row:
        raise _bad(404, "NOT_FOUND", "unknown question")
    resume = drafts.resume_text(user.id, body.resume_id)
    if not resume:
        raise _bad(400, "NO_RESUME", "add a resume under settings first")
    cfg = ai_access.require_config(user)
    instruction = {"role": "user", "kind": "instruction", "text": body.instruction, "at": _now()}
    row = db.query_one(
        "UPDATE application_answers SET draft_revision = draft_revision + 1, "
        "turns = turns || %s::jsonb, updated_at = now() "
        "WHERE user_id = %s AND job_id = %s AND key = %s RETURNING *",
        (db.jsonb([instruction]), user.id, job_id, key),
    )
    if row is None:
        raise _bad(404, "NOT_FOUND", "unknown question")
    with budget.record_parse_failures(user.id, cfg.key_source, drafts.PURPOSE, cfg.model):
        parsed, usage = await ai.parse(
            cfg,
            drafts.instructions(drafts.writing_style(user.id)),
            drafts.question_input(
                row["question"],
                job["company"],
                job["title"],
                get_content(job["url"]) or "",
                resume,
                draft=row["draft"],
                turns=row["turns"][:-1],
                instruction=body.instruction,
            ),
            drafts.Draft,
        )
    budget.record_tokens(user.id, cfg.key_source, drafts.PURPOSE, cfg.model, usage)
    if parsed is None:
        raise _bad(502, "NO_ANSWER", "the model returned no usable answer; try again")
    turns = [
        {"role": "assistant", "kind": "refine", "text": parsed.answer, "at": _now()},
    ]
    updated = db.query_one(
        f"""
        UPDATE application_answers
           SET draft = %s, model = %s, turns = turns || %s::jsonb, updated_at = now(),
               draft_revision = draft_revision + 1
         WHERE user_id = %s AND job_id = %s AND key = %s AND draft_revision = %s
        RETURNING {_ANSWER_COLS}
        """,
        (parsed.answer, cfg.model, json.dumps(turns), user.id, job_id, key, row["draft_revision"]),
    )
    if updated is None:
        raise _bad(
            409, "ANSWER_CHANGED", "the answer changed while refining; your newer answer was kept"
        )
    return updated
