"""Move older application report pages into durable chunks."""

from __future__ import annotations

import json
import uuid

from api import db
from api.apply.report_storage import REPORT_CHUNK_BYTES


def move_one_report() -> int | None:
    """Move one locked page and clear it only after its copy matches."""
    with db.transaction():
        row = db.query_one(
            "SELECT id, user_id, url, host, note, page FROM application_reports "
            "WHERE page IS NOT NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        if row is None:
            return None
        existing = db.query_one(
            "SELECT id FROM application_report_uploads WHERE report_id = %s", (row["id"],)
        )
        if existing is not None:
            raise RuntimeError(f"report {row['id']} already has an upload")
        page = row["page"]
        encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        chunks = [
            encoded[offset : offset + REPORT_CHUNK_BYTES]
            for offset in range(0, len(encoded), REPORT_CHUNK_BYTES)
        ]
        upload_id = uuid.uuid4()
        title = page.get("title") if isinstance(page, dict) else None
        fields = page.get("fields") if isinstance(page, dict) else None
        db.execute(
            "INSERT INTO application_report_uploads "
            "(id, user_id, url, host, note, title, field_count, chunk_count, report_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                upload_id,
                row["user_id"],
                row["url"],
                row["host"],
                row["note"],
                str(title)[:2000] if title is not None else None,
                len(fields) if isinstance(fields, list) else 0,
                len(chunks),
                row["id"],
            ),
        )
        db.executemany(
            "INSERT INTO application_report_chunks (upload_id, chunk_index, body) VALUES (%s, %s, %s)",
            [(upload_id, index, chunk) for index, chunk in enumerate(chunks)],
        )
        check = db.query_one(
            "SELECT convert_from(string_agg(c.body, ''::bytea ORDER BY c.chunk_index), 'UTF8')::jsonb "
            "= r.page AS exact FROM application_reports r "
            "JOIN application_report_chunks c ON c.upload_id = %s "
            "WHERE r.id = %s GROUP BY r.id",
            (upload_id, row["id"]),
        )
        if check is None or not check["exact"]:
            raise RuntimeError(f"report {row['id']} chunk copy did not match")
        db.execute("UPDATE application_reports SET page = NULL WHERE id = %s", (row["id"],))
        return row["id"]


def remaining_pages() -> int:
    row = db.query_one("SELECT count(*) AS count FROM application_reports WHERE page IS NOT NULL")
    return row["count"] if row else 0
