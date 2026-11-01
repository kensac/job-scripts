"""Extract a stored PDF after its upload is complete."""

from __future__ import annotations

import tempfile
import uuid
from typing import Any

lazy from pypdf import PdfReader

from api import db
from api.resumes.revisions import archive_resume, lock_owner
from api.resumes.text_versions import store_text
from core.payloads.blob_parts import BlobPart, BlobPartStore


def _parts(upload_id: uuid.UUID, first: int, count: int) -> list[dict[str, Any]]:
    return db.query(
        "SELECT part_index, sha256, size FROM resume_upload_parts "
        "WHERE upload_id = %s AND part_index >= %s "
        "ORDER BY part_index LIMIT %s",
        (upload_id, first, count),
    )


def _extract(upload_id: uuid.UUID, user_id: int, size: int, part_count: int) -> uuid.UUID:
    store = BlobPartStore.from_env()
    with tempfile.TemporaryFile() as pdf:
        next_part = 0
        written = 0
        while next_part < part_count:
            # Metadata stays bounded even when the file has many parts.
            rows = _parts(upload_id, next_part, 100)
            if not rows:
                raise RuntimeError("a stored PDF part is missing")
            for row in rows:
                if row["part_index"] != next_part:
                    raise RuntimeError("a stored PDF part is missing")
                part = BlobPart(upload_id, next_part, row["sha256"], row["size"])
                store.copy_to(part, pdf)
                written += part.size
                next_part += 1
        if written != size:
            raise RuntimeError("stored PDF size differs from its receipt")
        pdf.seek(0)
        reader = PdfReader(pdf)

        def page_text():
            for index, page in enumerate(reader.pages):
                if index:
                    yield "\n"
                yield page.extract_text() or ""

        version_id, _ = store_text(user_id, page_text())
    return version_id


def _save_resume(upload_id: uuid.UUID, row: dict[str, Any], version_id: uuid.UUID) -> None:
    with db.transaction():
        setting = db.query_one(
            "SELECT value FROM app_config WHERE key = 'resume_pdf_uploads_enabled' FOR SHARE"
        )
        if setting is None or setting["value"] is not True:
            raise RuntimeError("PDF uploads are paused")
        lock_owner(row["user_id"])
        locked = db.query_one(
            "SELECT status FROM resume_uploads WHERE id = %s FOR UPDATE", (upload_id,)
        )
        if locked is None or locked["status"] == "ready":
            return
        old = db.query_one(
            "SELECT id FROM user_resumes WHERE user_id = %s AND name = %s",
            (row["user_id"], row["name"]),
        )
        if old is not None:
            archive_resume(old["id"], row["user_id"])
        saved = db.query_one(
            """INSERT INTO user_resumes
               (user_id, name, filename, pdf_upload_id, text_version_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (user_id, name) DO UPDATE SET
                   filename = EXCLUDED.filename,
                   pdf_upload_id = EXCLUDED.pdf_upload_id,
                   text_version_id = EXCLUDED.text_version_id, updated_at = now()
               RETURNING id""",
            (row["user_id"], row["name"], row["filename"], upload_id, version_id),
        )
        assert saved is not None
        db.execute(
            "UPDATE resume_uploads SET status = 'ready', error = NULL, "
            "completed_resume_id = %s WHERE id = %s",
            (saved["id"], upload_id),
        )


async def handle_process_resume_upload(payload: dict[str, Any]) -> None:
    upload_id = uuid.UUID(payload["upload_id"])
    row = db.query_one(
        "SELECT user_id, name, filename, size, next_part, received_size, status "
        "FROM resume_uploads WHERE id = %s",
        (upload_id,),
    )
    if row is None or row["status"] == "ready":
        return
    if row["status"] not in ("processing", "failed") or row["received_size"] != row["size"]:
        raise RuntimeError("resume upload is not complete")
    db.execute(
        "UPDATE resume_uploads SET status = 'processing', error = NULL WHERE id = %s",
        (upload_id,),
    )
    try:
        if not db.get_config("resume_pdf_uploads_enabled"):
            raise RuntimeError("PDF uploads are paused")
        version_id = _extract(upload_id, row["user_id"], row["size"], row["next_part"])
        _save_resume(upload_id, row, version_id)
    except Exception as exc:
        db.execute(
            "UPDATE resume_uploads SET status = 'failed', error = %s "
            "WHERE id = %s AND status <> 'ready'",
            (str(exc), upload_id),
        )
        raise
