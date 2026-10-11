"""A person's resume library and its uploaded PDF files."""

from __future__ import annotations

import base64
import datetime
import io

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser, require_user
from api.models import Ok
from api.problem import SIZE_REFUSALS, refuse

router = APIRouter()

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_RESUME_CHARS = 60_000
_RESUME_COLS = (
    "id, name, filename, length(text) AS chars, text, pdf IS NOT NULL AS has_pdf, "
    "created_at, updated_at"
)


class Resume(BaseModel):
    """`_RESUME_COLS`, in types. `text` is what the drafts are written from;
    `pdf` is not here because the file is fetched from its own route."""

    id: int
    name: str
    filename: str | None
    chars: int
    text: str
    has_pdf: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ResumeList(BaseModel):
    resumes: list[Resume]


class ResumeRef(BaseModel):
    """Enough to choose one from a menu."""

    id: int
    name: str


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


@router.get("/user/resumes")
def list_resumes(user: AuthedUser = Depends(require_user)) -> ResumeList:
    return ResumeList(
        resumes=db.query_as(
            Resume,
            f"SELECT {_RESUME_COLS} FROM user_resumes WHERE user_id = %s ORDER BY updated_at DESC",
            (user.id,),
        )
    )


@router.post("/user/resumes", status_code=201, responses=SIZE_REFUSALS)
def create_resume(body: ResumeCreate, user: AuthedUser = Depends(require_user)) -> Resume:
    """Pasted text or a PDF, base64 in the body. The PDF's text is what is
    kept; the file itself is not stored. Same name replaces the text."""
    text = (body.text or "").strip()
    data = None
    if body.pdf_base64:
        try:
            data = base64.b64decode(body.pdf_base64, validate=True)
        except Exception as exc:
            raise refuse(400, "INVALID_PDF", "the attachment is not valid base64") from exc
        if len(data) > MAX_PDF_BYTES:
            raise refuse(413, "PDF_TOO_LARGE", "PDFs up to 5 MB")
        try:
            text = pdf_text(data)
        except Exception as exc:
            raise refuse(400, "INVALID_PDF", "could not read that PDF") from exc
        if not text:
            raise refuse(
                400, "NO_TEXT", "that PDF has no extractable text; paste the resume instead"
            )
    if not text:
        raise refuse(400, "NO_TEXT", "paste the resume text or attach a PDF")
    # The PDF's bytes live in the database and are dumped and archived with
    # it, so the count per person is bounded as well as the size per file.
    kept = db.query_one(
        "SELECT count(*) AS n FROM user_resumes WHERE user_id = %s AND name != %s",
        (user.id, body.name.strip()),
    )
    cap = int(db.get_config("resumes_per_user", 10))
    if kept and kept["n"] >= cap:
        raise refuse(409, "TOO_MANY_RESUMES", f"up to {cap} resumes; delete one first")
    row = db.query_one_as(
        Resume,
        f"""
        INSERT INTO user_resumes (user_id, name, text, filename, pdf)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id, name) DO UPDATE
            SET text = EXCLUDED.text, filename = EXCLUDED.filename, pdf = EXCLUDED.pdf,
                updated_at = now()
        RETURNING {_RESUME_COLS}
        """,
        (user.id, body.name.strip(), text, body.filename, data),
    )
    assert row is not None  # an upsert with RETURNING always yields its row
    return row


@router.get(
    "/user/resumes/{resume_id}/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def resume_pdf(resume_id: int, user: AuthedUser = Depends(require_user)) -> Response:
    """The file as uploaded, for the extension to attach to a form. A pasted
    resume has no file. Declared as a PDF because the schema said JSON, which
    was never true of this route."""

    row = db.query_one(
        "SELECT filename, pdf FROM user_resumes WHERE id = %s AND user_id = %s",
        (resume_id, user.id),
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown resume")
    if row["pdf"] is None:
        raise refuse(404, "NO_FILE", "this resume was pasted; upload the PDF to attach it")
    name = (row["filename"] or "resume.pdf").replace('"', "")
    return Response(
        bytes(row["pdf"]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@router.patch("/user/resumes/{resume_id}")
def patch_resume(
    resume_id: int, body: ResumePatch, user: AuthedUser = Depends(require_user)
) -> Resume:
    row = db.query_one_as(
        Resume,
        f"""
        UPDATE user_resumes
           SET name = COALESCE(%s, name), text = COALESCE(%s, text), updated_at = now()
         WHERE id = %s AND user_id = %s
        RETURNING {_RESUME_COLS}
        """,
        (body.name.strip() if body.name else None, body.text, resume_id, user.id),
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown resume")
    return row


@router.delete("/user/resumes/{resume_id}")
def delete_resume(resume_id: int, user: AuthedUser = Depends(require_user)) -> Ok:
    row = db.query_one(
        "DELETE FROM user_resumes WHERE id = %s AND user_id = %s RETURNING id",
        (resume_id, user.id),
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown resume")
    return Ok()
