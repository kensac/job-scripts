"""Select an old resume value and enqueue its next backfill step."""

from __future__ import annotations

import uuid
from typing import Any

from api import db, queue
from api.resumes.text_versions import TEXT_PART_CHARS
from core.payloads.blob_parts import PART_BYTES

TABLES = {"current": "user_resumes", "revision": "user_resume_revisions"}
POINTERS = {"text": "text_version_id", "pdf": "pdf_upload_id"}
SEED = "00" * 32


def source(kind: str, row_id: int, field: str, *, lock: bool = False) -> dict[str, Any] | None:
    table, pointer = TABLES[kind], POINTERS[field]
    length = "length" if field == "text" else "octet_length"
    return db.query_one(
        f"SELECT user_id, name, filename, xmin::text AS source_xmin, "
        f"{length}({field}) AS total, {pointer} AS pointer "
        f"FROM {table} WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (row_id,),
    )


def next_value() -> tuple[str, int, str] | None:
    candidates: list[tuple[bool, Any, str, int, str]] = []
    for field, pointer in POINTERS.items():
        if field == "pdf" and not db.get_config("resume_pdf_uploads_enabled"):
            continue
        for kind, table in TABLES.items():
            row = db.query_one(
                f"SELECT s.id, r.attempted_at FROM {table} s "
                "LEFT JOIN resume_backfill_runs r ON r.source_kind = %s "
                "AND r.source_id = s.id AND r.field = %s "
                "AND r.source_xmin = s.xmin::text "
                f"WHERE s.{field} IS NOT NULL AND s.{pointer} IS NULL "
                "ORDER BY r.attempted_at NULLS FIRST, s.id LIMIT 1",
                (kind, field),
            )
            if row is not None:
                candidates.append(
                    (row["attempted_at"] is not None, row["attempted_at"], kind, row["id"], field)
                )
    if not candidates:
        return None
    _, _, kind, row_id, field = min(candidates, key=lambda value: (value[0], value[1] or 0))
    return kind, row_id, field


def part_count(total: int, field: str) -> int:
    size = TEXT_PART_CHARS if field == "text" else PART_BYTES
    return (total + size - 1) // size


def schedule_next(cycle: str) -> None:
    candidate = next_value()
    if candidate is None:
        return
    kind, row_id, field = candidate
    with db.transaction():
        old = source(kind, row_id, field)
        if old is None or old["pointer"] is not None or old["total"] is None:
            return
        total = old["total"]
        xmin = old["source_xmin"]
        run_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"job-scripts/resume-backfill/v1/{kind}/{row_id}/{field}/{xmin}"
        )
        if field == "text":
            db.execute(
                "INSERT INTO resume_text_versions (id, user_id) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (run_id, old["user_id"]),
            )
        else:
            db.execute(
                "INSERT INTO resume_uploads (id, user_id, name, filename, size) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (run_id, old["user_id"], old["name"], old["filename"] or "resume.pdf", total),
            )
        db.execute(
            "INSERT INTO resume_backfill_runs "
            "(id, source_kind, source_id, field, source_xmin, total, part_count, "
            "source_digest, copy_digest, stage) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'copy') "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, kind, row_id, field, xmin, total, part_count(total, field), SEED, SEED),
        )
        run = db.query_one("SELECT stage FROM resume_backfill_runs WHERE id = %s", (run_id,))
        assert run is not None
        if run["stage"] != "done":
            db.execute(
                "UPDATE resume_backfill_runs SET attempted_at = now() WHERE id = %s", (run_id,)
            )
            queue.enqueue(
                "backfill_resumes",
                {"run_id": str(run_id)},
                dedupe_key=f"resume-backfill-resume:{run_id}:{cycle}",
            )
