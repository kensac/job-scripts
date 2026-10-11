"""Resumable page reports from the browser extension."""

from __future__ import annotations

import base64
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser, require_user
from api.problem import SIZE_REFUSALS, refuse
from api.routers.apply_routes.events import record_apply_event
from api.routers.apply_routes.models import ReportCreated

router = APIRouter()

# A request fits in memory. A report may contain any number of requests.
REPORT_CHUNK_BYTES = 64 * 1024


class ReportUploadStart(BaseModel):
    id: uuid.UUID
    url: str = Field(min_length=1, max_length=2000)
    note: str = Field(default="", max_length=4000)
    title: str | None = Field(default=None, max_length=2000)
    field_count: int = Field(ge=0)
    chunk_count: int = Field(ge=1)


class ReportUploadReceipt(BaseModel):
    id: uuid.UUID
    chunk_count: int
    received_chunks: int
    report_id: int | None


class ReportChunk(BaseModel):
    index: int
    data: str


def _owned_upload(upload_id: uuid.UUID, user_id: int, *, lock: bool = False) -> dict:
    row = db.query_one(
        "SELECT id, url, host, note, title, field_count, chunk_count, report_id "
        "FROM application_report_uploads "
        "WHERE id = %s AND user_id = %s" + (" FOR UPDATE" if lock else ""),
        (upload_id, user_id),
    )
    if row is None:
        raise refuse(404, "NOT_FOUND", "unknown report upload")
    return row


def _receipt(upload_id: uuid.UUID, user_id: int) -> ReportUploadReceipt:
    row = _owned_upload(upload_id, user_id)
    count = db.query_one(
        "SELECT count(*) AS count FROM application_report_chunks WHERE upload_id = %s",
        (upload_id,),
    )
    return ReportUploadReceipt(
        id=upload_id,
        chunk_count=row["chunk_count"],
        received_chunks=count["count"] if count else 0,
        report_id=row["report_id"],
    )


@router.post("/user/apply/report-uploads", status_code=201)
def start_report_upload(
    body: ReportUploadStart, user: AuthedUser = Depends(require_user)
) -> ReportUploadReceipt:
    with db.transaction():
        db.execute(
            "INSERT INTO application_report_uploads "
            "(id, user_id, url, host, note, title, field_count, chunk_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                body.id,
                user.id,
                body.url,
                urlsplit(body.url).netloc,
                body.note,
                body.title,
                body.field_count,
                body.chunk_count,
            ),
        )
        stored = _owned_upload(body.id, user.id)
        if (
            stored["url"],
            stored["note"],
            stored["title"],
            stored["field_count"],
            stored["chunk_count"],
        ) != (
            body.url,
            body.note,
            body.title,
            body.field_count,
            body.chunk_count,
        ):
            raise refuse(409, "UPLOAD_CONFLICT", "report upload details changed")
        return _receipt(body.id, user.id)


@router.get("/user/apply/report-uploads/{upload_id}")
def get_report_upload(
    upload_id: uuid.UUID, user: AuthedUser = Depends(require_user)
) -> ReportUploadReceipt:
    return _receipt(upload_id, user.id)


@router.put("/user/apply/report-uploads/{upload_id}/chunks/{chunk_index}", responses=SIZE_REFUSALS)
async def put_report_chunk(
    upload_id: uuid.UUID,
    chunk_index: int,
    request: Request,
    user: AuthedUser = Depends(require_user),
) -> ReportUploadReceipt:
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > REPORT_CHUNK_BYTES:
            raise refuse(413, "REPORT_CHUNK_TOO_LARGE", "report chunk exceeds 64 KiB")
        data.extend(part)
    if not data:
        raise refuse(400, "EMPTY_CHUNK", "report chunk is empty")
    with db.transaction():
        upload = _owned_upload(upload_id, user.id, lock=True)
        if chunk_index < 0 or chunk_index >= upload["chunk_count"]:
            raise refuse(400, "INVALID_CHUNK", "chunk index is outside this upload")
        existing = db.query_one(
            "SELECT body FROM application_report_chunks WHERE upload_id = %s AND chunk_index = %s",
            (upload_id, chunk_index),
        )
        if existing is not None:
            if bytes(existing["body"]) != data:
                raise refuse(409, "CHUNK_CONFLICT", "chunk bytes changed")
        elif upload["report_id"] is not None:
            raise refuse(409, "MISSING_CHUNK", "completed report is missing a chunk")
        else:
            db.execute(
                "INSERT INTO application_report_chunks (upload_id, chunk_index, body) "
                "VALUES (%s, %s, %s)",
                (upload_id, chunk_index, bytes(data)),
            )
        return _receipt(upload_id, user.id)


@router.post("/user/apply/report-uploads/{upload_id}/complete", status_code=201)
def complete_report_upload(
    upload_id: uuid.UUID, user: AuthedUser = Depends(require_user)
) -> ReportCreated:
    with db.transaction():
        upload = _owned_upload(upload_id, user.id, lock=True)
        if upload["report_id"] is not None:
            existing = db.query_one_as(
                ReportCreated,
                "SELECT id, created_at FROM application_reports WHERE id = %s",
                (upload["report_id"],),
            )
            assert existing is not None
            return existing
        count = db.query_one(
            "SELECT count(*) AS count FROM application_report_chunks WHERE upload_id = %s",
            (upload_id,),
        )
        if count is None or count["count"] != upload["chunk_count"]:
            raise refuse(409, "INCOMPLETE_REPORT", "report chunks are still missing")
        # Chunks remain the only copy of the complete page.
        report = db.query_one_as(
            ReportCreated,
            "INSERT INTO application_reports (user_id, url, host, note, page) "
            "VALUES (%s, %s, %s, %s, NULL) RETURNING id, created_at",
            (user.id, upload["url"], upload["host"], upload["note"]),
        )
        assert report is not None
        db.execute(
            "UPDATE application_report_uploads SET report_id = %s WHERE id = %s",
            (report.id, upload_id),
        )
    record_apply_event(
        "apply_page_reported", user, host=upload["host"], has_note=bool(upload["note"].strip())
    )
    return report


@router.get("/user/apply/reports/{report_id}/chunks/{chunk_index}")
def get_report_chunk(
    report_id: int,
    chunk_index: int,
    user: AuthedUser = Depends(require_user),
) -> ReportChunk:
    row = db.query_one(
        "SELECT c.body FROM application_reports r "
        "JOIN application_report_uploads u ON u.report_id = r.id "
        "JOIN application_report_chunks c ON c.upload_id = u.id "
        "WHERE r.id = %s AND r.user_id = %s AND c.chunk_index = %s",
        (report_id, user.id, chunk_index),
    )
    if row is None:
        raise refuse(404, "NOT_FOUND", "unknown report chunk")
    return ReportChunk(index=chunk_index, data=base64.b64encode(row["body"]).decode("ascii"))
