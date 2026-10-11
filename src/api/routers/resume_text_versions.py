"""Read and edit complete resume text in bounded parts."""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser, require_user
from api.problem import refuse
from api.resumes.revisions import archive_resume, lock_owner

router = APIRouter()
TEXT_PART_BYTES = 131_072


class TextVersionStart(BaseModel):
    id: uuid.UUID


class TextVersionComplete(BaseModel):
    part_count: int = Field(gt=0)
    chars: int = Field(gt=0)
    base_updated_at: datetime.datetime


class NewTextComplete(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    part_count: int = Field(gt=0)
    chars: int = Field(gt=0)


class CompletedResume(BaseModel):
    resume_id: int


class TextVersionInfo(BaseModel):
    version_id: uuid.UUID | None
    chars: int
    part_count: int
    updated_at: datetime.datetime


class TextPart(BaseModel):
    text: str


def _resume(resume_id: int, user_id: int) -> dict:
    row = db.query_one(
        "SELECT id, text, text_version_id, updated_at FROM user_resumes "
        "WHERE id = %s AND user_id = %s",
        (resume_id, user_id),
    )
    if row is None:
        raise refuse(404, "NOT_FOUND", "unknown resume")
    return row


@router.get("/user/resumes/{resume_id}/text")
def text_info(resume_id: int, user: AuthedUser = Depends(require_user)) -> TextVersionInfo:
    row = _resume(resume_id, user.id)
    if row["text_version_id"] is None:
        return TextVersionInfo(
            version_id=None,
            chars=len(row["text"]),
            part_count=1,
            updated_at=row["updated_at"],
        )
    version = db.query_one(
        "SELECT chars, part_count FROM resume_text_versions WHERE id = %s AND ready",
        (row["text_version_id"],),
    )
    if version is None:
        raise refuse(503, "TEXT_UNAVAILABLE", "the saved resume text is unavailable")
    return TextVersionInfo(
        version_id=row["text_version_id"],
        chars=version["chars"],
        part_count=version["part_count"],
        updated_at=row["updated_at"],
    )


@router.get("/user/resumes/{resume_id}/text/parts/{index}")
def text_part(resume_id: int, index: int, user: AuthedUser = Depends(require_user)) -> TextPart:
    if index < 0:
        raise refuse(404, "NOT_FOUND", "unknown text part")
    row = _resume(resume_id, user.id)
    if row["text_version_id"] is None:
        if index:
            raise refuse(404, "NOT_FOUND", "unknown text part")
        return TextPart(text=row["text"])
    part = db.query_one(
        "SELECT p.text FROM resume_text_parts p JOIN resume_text_versions v "
        "ON v.id = p.version_id WHERE p.version_id = %s AND p.part_index = %s AND v.ready",
        (row["text_version_id"], index),
    )
    if part is None:
        raise refuse(404, "NOT_FOUND", "unknown text part")
    return TextPart(**part)


@router.post("/user/resumes/{resume_id}/text-versions", status_code=201)
def start_text_version(
    resume_id: int, user: AuthedUser = Depends(require_user)
) -> TextVersionStart:
    _resume(resume_id, user.id)
    version_id = uuid.uuid4()
    db.execute(
        "INSERT INTO resume_text_versions (id, user_id) VALUES (%s, %s)",
        (version_id, user.id),
    )
    return TextVersionStart(id=version_id)


@router.post("/user/resume-text-versions", status_code=201)
def start_new_text_version(user: AuthedUser = Depends(require_user)) -> TextVersionStart:
    version_id = uuid.uuid4()
    db.execute(
        "INSERT INTO resume_text_versions (id, user_id) VALUES (%s, %s)",
        (version_id, user.id),
    )
    return TextVersionStart(id=version_id)


@router.put("/user/resumes/{resume_id}/text-versions/{version_id}/parts/{index}")
async def put_text_part(
    resume_id: int,
    version_id: uuid.UUID,
    index: int,
    request: Request,
    user: AuthedUser = Depends(require_user),
) -> TextPart:
    _resume(resume_id, user.id)
    return await _write_part(version_id, index, request, user)


@router.put("/user/resume-text-versions/{version_id}/parts/{index}")
async def put_new_text_part(
    version_id: uuid.UUID,
    index: int,
    request: Request,
    user: AuthedUser = Depends(require_user),
) -> TextPart:
    return await _write_part(version_id, index, request, user)


async def _write_part(
    version_id: uuid.UUID, index: int, request: Request, user: AuthedUser
) -> TextPart:
    if index < 0:
        raise refuse(400, "PART_ORDER", "part number must be nonnegative")
    data = bytearray()
    async for chunk in request.stream():
        if len(chunk) > TEXT_PART_BYTES - len(data):
            raise refuse(413, "PART_TOO_LARGE", "send text in smaller parts")
        data.extend(chunk)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refuse(400, "INVALID_TEXT", "text must be UTF-8") from exc
    if not text:
        raise refuse(400, "EMPTY_PART", "a text part needs characters")
    with db.transaction():
        version = db.query_one(
            "SELECT ready FROM resume_text_versions WHERE id = %s AND user_id = %s FOR UPDATE",
            (version_id, user.id),
        )
        if version is None:
            raise refuse(404, "NOT_FOUND", "unknown text version")
        old = db.query_one(
            "SELECT text FROM resume_text_parts WHERE version_id = %s AND part_index = %s",
            (version_id, index),
        )
        if old is not None:
            if old["text"] != text:
                raise refuse(409, "PART_CHANGED", "this part already has different text")
            return TextPart(text=text)
        if version["ready"]:
            raise refuse(409, "VERSION_COMPLETE", "this text version is complete")
        db.execute(
            "INSERT INTO resume_text_parts (version_id, part_index, text) VALUES (%s, %s, %s)",
            (version_id, index, text),
        )
    return TextPart(text=text)


def _complete_parts(version_id: uuid.UUID, part_count: int, chars: int) -> None:
    parts = db.query_one(
        "SELECT count(*) AS n, min(part_index) AS first, max(part_index) AS last, "
        "coalesce(sum(length(text)), 0) AS chars FROM resume_text_parts "
        "WHERE version_id = %s",
        (version_id,),
    )
    if (
        parts is None
        or parts["n"] != part_count
        or parts["first"] != 0
        or parts["last"] != part_count - 1
        or parts["chars"] != chars
    ):
        raise refuse(409, "TEXT_INCOMPLETE", "send every text part before saving")
    db.execute(
        "UPDATE resume_text_versions SET ready = TRUE, chars = %s, part_count = %s WHERE id = %s",
        (chars, part_count, version_id),
    )


@router.post("/user/resume-text-versions/{version_id}/complete")
def complete_new_text_version(
    version_id: uuid.UUID,
    body: NewTextComplete,
    user: AuthedUser = Depends(require_user),
) -> CompletedResume:
    with db.transaction():
        lock_owner(user.id)
        version = db.query_one(
            "SELECT ready FROM resume_text_versions WHERE id = %s AND user_id = %s FOR UPDATE",
            (version_id, user.id),
        )
        if version is None:
            raise refuse(404, "NOT_FOUND", "unknown text version")
        if version["ready"]:
            current = db.query_one(
                "SELECT id FROM user_resumes WHERE user_id = %s AND text_version_id = %s",
                (user.id, version_id),
            )
            if current is None:
                raise refuse(409, "VERSION_COMPLETE", "this text version was already saved")
            return CompletedResume(resume_id=current["id"])
        _complete_parts(version_id, body.part_count, body.chars)
        old = db.query_one(
            "SELECT id FROM user_resumes WHERE user_id = %s AND name = %s",
            (user.id, body.name.strip()),
        )
        if old is not None:
            archive_resume(old["id"], user.id)
        saved = db.query_one(
            """INSERT INTO user_resumes (user_id, name, text, text_version_id)
               VALUES (%s, %s, '', %s)
               ON CONFLICT (user_id, name) DO UPDATE SET
                   text = '', text_version_id = EXCLUDED.text_version_id,
                   filename = NULL, pdf = NULL, pdf_upload_id = NULL, updated_at = now()
               RETURNING id""",
            (user.id, body.name.strip(), version_id),
        )
        assert saved is not None
        return CompletedResume(resume_id=saved["id"])


@router.post("/user/resumes/{resume_id}/text-versions/{version_id}/complete")
def complete_text_version(
    resume_id: int,
    version_id: uuid.UUID,
    body: TextVersionComplete,
    user: AuthedUser = Depends(require_user),
) -> TextVersionInfo:
    with db.transaction():
        lock_owner(user.id)
        row = _resume(resume_id, user.id)
        version = db.query_one(
            "SELECT ready, chars, part_count FROM resume_text_versions "
            "WHERE id = %s AND user_id = %s FOR UPDATE",
            (version_id, user.id),
        )
        if version is None:
            raise refuse(404, "NOT_FOUND", "unknown text version")
        if version["ready"] and row["text_version_id"] == version_id:
            return TextVersionInfo(
                version_id=version_id,
                chars=version["chars"],
                part_count=version["part_count"],
                updated_at=row["updated_at"],
            )
        if version["ready"] or row["updated_at"] != body.base_updated_at:
            raise refuse(409, "RESUME_CHANGED", "reload the resume before saving")
        _complete_parts(version_id, body.part_count, body.chars)
        archive_resume(resume_id, user.id)
        updated = db.query_one(
            "UPDATE user_resumes SET text = '', text_version_id = %s, updated_at = now() "
            "WHERE id = %s RETURNING updated_at",
            (version_id, resume_id),
        )
        assert updated is not None
        return TextVersionInfo(
            version_id=version_id,
            chars=body.chars,
            part_count=body.part_count,
            updated_at=updated["updated_at"],
        )
