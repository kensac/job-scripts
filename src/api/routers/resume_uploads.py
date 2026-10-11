"""Resumable PDF transfer backed by verified object parts."""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api import db, queue
from api.auth import AuthedUser, require_user
from api.problem import refuse
from core.payloads.blob_parts import PART_BYTES, BlobPartStore

router = APIRouter()


class ResumeUploadStart(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0)


class ResumeUploadState(BaseModel):
    id: uuid.UUID
    status: str
    size: int
    received_size: int
    next_part: int
    completed_resume_id: int | None
    error: str | None


_COLUMNS = "id, status, size, received_size, next_part, completed_resume_id, error"


def _upload(upload_id: uuid.UUID, user_id: int, *, lock: bool = False) -> dict | None:
    return db.query_one(
        f"SELECT {_COLUMNS} FROM resume_uploads WHERE id = %s AND user_id = %s"
        + (" FOR UPDATE" if lock else ""),
        (upload_id, user_id),
    )


@router.post("/user/resume-uploads", status_code=201)
def start_upload(
    body: ResumeUploadStart, user: AuthedUser = Depends(require_user)
) -> ResumeUploadState:
    if not db.get_config("resume_pdf_uploads_enabled"):
        raise refuse(503, "PDF_UPLOADS_PAUSED", "PDF uploads are paused")
    upload_id = uuid.uuid4()
    row = db.query_one(
        f"INSERT INTO resume_uploads (id, user_id, name, filename, size) "
        f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
        (upload_id, user.id, body.name.strip(), body.filename, body.size),
    )
    assert row is not None
    return ResumeUploadState(**row)


@router.get("/user/resume-uploads/{upload_id}")
def upload_state(
    upload_id: uuid.UUID, user: AuthedUser = Depends(require_user)
) -> ResumeUploadState:
    row = _upload(upload_id, user.id)
    if row is None:
        raise refuse(404, "NOT_FOUND", "unknown upload")
    return ResumeUploadState(**row)


def _store_part(upload_id: uuid.UUID, index: int, data: bytes, user_id: int) -> ResumeUploadState:
    digest = hashlib.sha256(data).hexdigest()
    replay = False
    with db.transaction():
        row = _upload(upload_id, user_id, lock=True)
        if row is None:
            raise refuse(404, "NOT_FOUND", "unknown upload")
        if row["status"] != "receiving":
            raise refuse(409, "UPLOAD_CLOSED", "this upload is no longer receiving parts")
        if index > row["next_part"]:
            raise refuse(409, "PART_ORDER", "send the next part first")
        if index < row["next_part"]:
            saved = db.query_one(
                "SELECT sha256, size, verified FROM resume_upload_parts "
                "WHERE upload_id = %s AND part_index = %s",
                (upload_id, index),
            )
            if not saved or saved["sha256"] != digest or saved["size"] != len(data):
                raise refuse(409, "PART_CHANGED", "this part already has different bytes")
            replay = True
        else:
            if row["received_size"] + len(data) > row["size"]:
                raise refuse(409, "SIZE_MISMATCH", "parts exceed the declared file size")
            saved = db.query_one(
                "SELECT sha256, size FROM resume_upload_parts "
                "WHERE upload_id = %s AND part_index = %s",
                (upload_id, index),
            )
            if saved and (saved["sha256"] != digest or saved["size"] != len(data)):
                raise refuse(409, "PART_CHANGED", "this part already has different bytes")
            if not saved:
                db.execute(
                    "INSERT INTO resume_upload_parts (upload_id, part_index, sha256, size) "
                    "VALUES (%s, %s, %s, %s)",
                    (upload_id, index, digest, len(data)),
                )
    # A failed object write leaves a pending receipt. The same bytes can retry.
    BlobPartStore.from_env().put(upload_id, index, data)
    if replay:
        return ResumeUploadState(**row)
    with db.transaction():
        row = _upload(upload_id, user_id, lock=True)
        assert row is not None
        if row["status"] != "receiving":
            raise refuse(409, "UPLOAD_CLOSED", "this upload is no longer receiving parts")
        if index == row["next_part"]:
            db.execute(
                "UPDATE resume_upload_parts SET verified = TRUE "
                "WHERE upload_id = %s AND part_index = %s",
                (upload_id, index),
            )
            row = db.query_one(
                f"UPDATE resume_uploads SET next_part = next_part + 1, "
                f"received_size = received_size + %s WHERE id = %s RETURNING {_COLUMNS}",
                (len(data), upload_id),
            )
            assert row is not None
        return ResumeUploadState(**row)


@router.put("/user/resume-uploads/{upload_id}/parts/{index}")
async def put_part(
    upload_id: uuid.UUID,
    index: int,
    request: Request,
    user: AuthedUser = Depends(require_user),
) -> ResumeUploadState:
    if index < 0:
        raise refuse(400, "PART_ORDER", "part number must be nonnegative")
    data = bytearray()
    async for chunk in request.stream():
        if len(chunk) > PART_BYTES - len(data):
            raise refuse(413, "PART_TOO_LARGE", "send this file in smaller parts")
        data.extend(chunk)
    if not data:
        raise refuse(400, "EMPTY_PART", "a part needs bytes")
    return await run_in_threadpool(_store_part, upload_id, index, bytes(data), user.id)


@router.post("/user/resume-uploads/{upload_id}/complete")
def complete_upload(
    upload_id: uuid.UUID, user: AuthedUser = Depends(require_user)
) -> ResumeUploadState:
    if not db.get_config("resume_pdf_uploads_enabled"):
        raise refuse(503, "PDF_UPLOADS_PAUSED", "PDF uploads are paused")
    with db.transaction():
        row = _upload(upload_id, user.id, lock=True)
        if row is None:
            raise refuse(404, "NOT_FOUND", "unknown upload")
        if row["status"] in ("receiving", "failed"):
            if row["received_size"] != row["size"]:
                raise refuse(409, "UPLOAD_INCOMPLETE", "send the remaining parts first")
            row = db.query_one(
                f"UPDATE resume_uploads SET status = 'processing', error = NULL WHERE id = %s "
                f"RETURNING {_COLUMNS}",
                (upload_id,),
            )
            queue.enqueue(
                "process_resume_upload",
                {"upload_id": str(upload_id)},
            )
        assert row is not None
        return ResumeUploadState(**row)
