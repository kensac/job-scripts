"""A person's resume library and its uploaded PDF files."""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from api import db
from api.auth import AuthedUser, require_user
from api.models import Ok
from api.problem import refuse
from api.resumes.revisions import archive_resume, lock_owner
from core.payloads.blob_parts import BlobPart, BlobPartStore

router = APIRouter()

MAX_NAME_CHARS = 120
# A character can use two JSON Unicode escapes.
MAX_RENAME_BODY_BYTES = MAX_NAME_CHARS * 12 + 32
_RESUME_COLS = (
    "id, name, filename, COALESCE((SELECT chars FROM resume_text_versions "
    "WHERE id = text_version_id), length(text)) AS chars, text, text_version_id, "
    "(pdf IS NOT NULL OR pdf_upload_id IS NOT NULL) AS has_pdf, "
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
    text_version_id: uuid.UUID | None
    has_pdf: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ResumeList(BaseModel):
    resumes: list[Resume]


class ResumeRef(BaseModel):
    """Enough to choose one from a menu."""

    id: int
    name: str


class ResumeRename(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)


@router.get("/user/resumes")
def list_resumes(user: AuthedUser = Depends(require_user)) -> ResumeList:
    return ResumeList(
        resumes=db.query_as(
            Resume,
            f"SELECT {_RESUME_COLS} FROM user_resumes WHERE user_id = %s ORDER BY updated_at DESC",
            (user.id,),
        )
    )


@router.post("/user/resumes", status_code=410)
def legacy_create_resume(user: AuthedUser = Depends(require_user)) -> None:
    raise refuse(410, "USE_RESUME_PARTS", "send resume text or PDF in numbered parts")


def _pdf_parts(upload_id: uuid.UUID, part_count: int) -> Iterator[bytes]:
    store = BlobPartStore.from_env()
    for part_index in range(part_count):
        row = db.query_one(
            "SELECT sha256, size FROM resume_upload_parts "
            "WHERE upload_id = %s AND part_index = %s AND verified",
            (upload_id, part_index),
        )
        if row is None:
            raise RuntimeError("stored PDF part is missing")
        yield store.read(BlobPart(upload_id, part_index, row["sha256"], row["size"]))


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
        "SELECT filename, pdf, pdf_upload_id FROM user_resumes WHERE id = %s AND user_id = %s",
        (resume_id, user.id),
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown resume")
    if row["pdf"] is None and row["pdf_upload_id"] is None:
        raise refuse(404, "NO_FILE", "this resume was pasted; upload the PDF to attach it")
    name = (row["filename"] or "resume.pdf").replace('"', "")
    if row["pdf_upload_id"] is not None:
        upload = db.query_one(
            "SELECT size, next_part FROM resume_uploads WHERE id = %s AND status = 'ready'",
            (row["pdf_upload_id"],),
        )
        if upload is None:
            raise refuse(503, "FILE_UNAVAILABLE", "the stored PDF is unavailable")
        return StreamingResponse(
            _pdf_parts(row["pdf_upload_id"], upload["next_part"]),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{name}"',
                "Content-Length": str(upload["size"]),
            },
        )
    pdf_data = row["pdf"]
    assert pdf_data is not None
    return Response(
        bytes(pdf_data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


def _rename_resume(resume_id: int, name: str, user_id: int) -> Resume:
    with db.transaction():
        lock_owner(user_id)
        old = db.query_one(
            "SELECT id FROM user_resumes WHERE id = %s AND user_id = %s",
            (resume_id, user_id),
        )
        if old is None:
            raise refuse(404, "NOT_FOUND", "unknown resume")
        occupied = db.query_one(
            "SELECT id FROM user_resumes WHERE user_id = %s AND name = %s",
            (user_id, name),
        )
        if occupied is not None and occupied["id"] != resume_id:
            raise refuse(409, "NAME_EXISTS", "another resume has that name")
        archive_resume(resume_id, user_id)
        row = db.query_one_as(
            Resume,
            f"""
        UPDATE user_resumes
           SET name = %s, updated_at = now()
         WHERE id = %s AND user_id = %s
        RETURNING {_RESUME_COLS}
        """,
            (name, resume_id, user_id),
        )
    assert row is not None
    return row


@router.patch("/user/resumes/{resume_id}")
async def patch_resume(
    resume_id: int, request: Request, user: AuthedUser = Depends(require_user)
) -> Resume:
    data = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_RENAME_BODY_BYTES - len(data):
            raise refuse(413, "RENAME_TOO_LARGE", "send only the resume name")
        data.extend(chunk)
    try:
        body = json.loads(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise refuse(400, "INVALID_NAME", "send a resume name") from exc
    if not isinstance(body, dict):
        raise refuse(400, "INVALID_NAME", "send a resume name")
    if "text" in body or "pdf_base64" in body:
        raise refuse(410, "USE_RESUME_PARTS", "send resume text or PDF in numbered parts")
    if set(body) != {"name"}:
        raise refuse(400, "INVALID_NAME", "send only the resume name")
    try:
        name = ResumeRename.model_validate(body).name.strip()
    except ValidationError as exc:
        raise refuse(400, "INVALID_NAME", "send a resume name") from exc
    if not name:
        raise refuse(400, "INVALID_NAME", "send a resume name")
    return await run_in_threadpool(_rename_resume, resume_id, name, user.id)


@router.delete("/user/resumes/{resume_id}")
def delete_resume(resume_id: int, user: AuthedUser = Depends(require_user)) -> Ok:
    with db.transaction():
        lock_owner(user.id)
        old = db.query_one(
            "SELECT id FROM user_resumes WHERE id = %s AND user_id = %s",
            (resume_id, user.id),
        )
        if old is None:
            raise refuse(404, "NOT_FOUND", "unknown resume")
        archive_resume(resume_id, user.id)
        row = db.query_one(
            "DELETE FROM user_resumes WHERE id = %s AND user_id = %s RETURNING id",
            (resume_id, user.id),
        )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown resume")
    return Ok()
