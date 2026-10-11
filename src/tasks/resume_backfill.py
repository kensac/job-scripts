"""Copy and verify one old resume part per durable Hetzner task."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from typing import Any

from api import db, queue
from api.resumes.text_versions import TEXT_PART_CHARS
from core.payloads.blob_parts import PART_BYTES, BlobPart, BlobPartStore

TABLES = {"current": "user_resumes", "revision": "user_resume_revisions"}
POINTERS = {"text": "text_version_id", "pdf": "pdf_upload_id"}
SEED = "00" * 32


def fold(previous: str, index: int, value: bytes) -> str:
    return hashlib.sha256(
        bytes.fromhex(previous) + index.to_bytes(8, "big") + len(value).to_bytes(8, "big") + value
    ).hexdigest()


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
    for field, pointer in POINTERS.items():
        if field == "pdf" and not db.get_config("resume_pdf_uploads_enabled"):
            continue
        for kind, table in TABLES.items():
            row = db.query_one(
                f"SELECT id FROM {table} WHERE {field} IS NOT NULL "
                f"AND {pointer} IS NULL ORDER BY id LIMIT 1"
            )
            if row is not None:
                return kind, row["id"], field
    return None


def part_count(total: int, field: str) -> int:
    size = TEXT_PART_CHARS if field == "text" else PART_BYTES
    return (total + size - 1) // size


def queue_step(run_id: uuid.UUID, stage: str, index: int) -> None:
    queue.enqueue(
        "backfill_resumes",
        {"run_id": str(run_id)},
        dedupe_key=f"resume-backfill:{run_id}:{stage}:{index}",
    )


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
            queue.enqueue(
                "backfill_resumes",
                {"run_id": str(run_id)},
                dedupe_key=f"resume-backfill-resume:{run_id}:{cycle}",
            )


def run_row(run_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    row = db.query_one(
        "SELECT source_kind, source_id, field, source_xmin, total, part_count, "
        "next_part, verify_part, source_digest, copy_digest, stage "
        "FROM resume_backfill_runs WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (run_id,),
    )
    if row is None or row["source_kind"] not in TABLES or row["field"] not in POINTERS:
        raise RuntimeError("invalid resume backfill run")
    return row


def matching_source(run: dict[str, Any], *, lock: bool = False) -> None:
    old = source(run["source_kind"], run["source_id"], run["field"], lock=lock)
    if (
        old is None
        or old["pointer"] is not None
        or old["total"] != run["total"]
        or old["source_xmin"] != run["source_xmin"]
    ):
        raise RuntimeError("resume source changed during backfill")


def source_part(run: dict[str, Any], index: int) -> str | bytes:
    field = run["field"]
    table = TABLES[run["source_kind"]]
    size = TEXT_PART_CHARS if field == "text" else PART_BYTES
    row = db.query_one(
        f"SELECT substring({field} from %s for %s) AS value FROM {table} "
        "WHERE id = %s AND xmin::text = %s",
        (index * size + 1, size, run["source_id"], run["source_xmin"]),
    )
    if row is None or row["value"] is None:
        raise RuntimeError("resume source changed during backfill")
    return row["value"] if field == "text" else bytes(row["value"])


def copy_text(run_id: uuid.UUID, run: dict[str, Any]) -> None:
    index = run["next_part"]
    value = source_part(run, index)
    assert isinstance(value, str)
    with db.transaction():
        current = run_row(run_id, lock=True)
        if current["stage"] != "copy" or current["next_part"] != index:
            return
        matching_source(current)
        db.execute(
            "INSERT INTO resume_text_parts (version_id, part_index, text) "
            "VALUES (%s, %s, %s) ON CONFLICT (version_id, part_index) DO NOTHING",
            (run_id, index, value),
        )
        saved = db.query_one(
            "SELECT text FROM resume_text_parts WHERE version_id = %s AND part_index = %s",
            (run_id, index),
        )
        if saved is None or saved["text"] != value:
            raise RuntimeError("resume text part differs from its source")
        following = index + 1
        stage = "verify" if following == run["part_count"] else "copy"
        db.execute(
            "UPDATE resume_backfill_runs SET next_part = %s, stage = %s WHERE id = %s",
            (following, stage, run_id),
        )
        queue_step(run_id, stage, 0 if stage == "verify" else following)


def copy_pdf(run_id: uuid.UUID, run: dict[str, Any]) -> None:
    if not db.get_config("resume_pdf_uploads_enabled"):
        raise RuntimeError("PDF object storage is paused")
    index = run["next_part"]
    value = source_part(run, index)
    assert isinstance(value, bytes)
    part = BlobPartStore.from_env().put(run_id, index, value)
    with db.transaction():
        current = run_row(run_id, lock=True)
        if current["stage"] != "copy" or current["next_part"] != index:
            return
        matching_source(current)
        db.execute(
            "INSERT INTO resume_upload_parts (upload_id, part_index, sha256, size, verified) "
            "VALUES (%s, %s, %s, %s, TRUE) ON CONFLICT (upload_id, part_index) DO NOTHING",
            (run_id, index, part.sha256, part.size),
        )
        saved = db.query_one(
            "SELECT sha256, size, verified FROM resume_upload_parts "
            "WHERE upload_id = %s AND part_index = %s",
            (run_id, index),
        )
        if saved != {"sha256": part.sha256, "size": part.size, "verified": True}:
            raise RuntimeError("resume PDF receipt differs from its source")
        following = index + 1
        stage = "verify" if following == run["part_count"] else "copy"
        db.execute(
            "UPDATE resume_uploads SET next_part = %s, received_size = received_size + %s "
            "WHERE id = %s",
            (following, part.size, run_id),
        )
        db.execute(
            "UPDATE resume_backfill_runs SET next_part = %s, stage = %s WHERE id = %s",
            (following, stage, run_id),
        )
        queue_step(run_id, stage, 0 if stage == "verify" else following)


def verify_part(run_id: uuid.UUID, run: dict[str, Any]) -> None:
    index = run["verify_part"]
    original = source_part(run, index)
    if run["field"] == "text":
        assert isinstance(original, str)
        saved = db.query_one(
            "SELECT text FROM resume_text_parts WHERE version_id = %s AND part_index = %s",
            (run_id, index),
        )
        if saved is None:
            raise RuntimeError("resume text part is missing")
        source_bytes = original.encode("utf-8")
        copy_bytes = saved["text"].encode("utf-8")
    else:
        assert isinstance(original, bytes)
        if not db.get_config("resume_pdf_uploads_enabled"):
            raise RuntimeError("PDF object storage is paused")
        saved = db.query_one(
            "SELECT sha256, size, verified FROM resume_upload_parts "
            "WHERE upload_id = %s AND part_index = %s",
            (run_id, index),
        )
        if saved is None or not saved["verified"]:
            raise RuntimeError("resume PDF part is missing")
        source_bytes = bytes(original)
        copy_bytes = BlobPartStore.from_env().read(
            BlobPart(run_id, index, saved["sha256"], saved["size"])
        )
    with db.transaction():
        current = run_row(run_id, lock=True)
        if current["stage"] != "verify" or current["verify_part"] != index:
            return
        matching_source(current)
        source_digest = fold(current["source_digest"], index, source_bytes)
        copy_digest = fold(current["copy_digest"], index, copy_bytes)
        if source_digest != copy_digest:
            raise RuntimeError("resume copy differs from its source")
        following = index + 1
        stage = "finalize" if following == current["part_count"] else "verify"
        db.execute(
            "UPDATE resume_backfill_runs SET verify_part = %s, source_digest = %s, "
            "copy_digest = %s, stage = %s WHERE id = %s",
            (following, source_digest, copy_digest, stage, run_id),
        )
        queue_step(run_id, stage, 0 if stage == "finalize" else following)


def finalize(run_id: uuid.UUID) -> None:
    with db.transaction():
        run = run_row(run_id, lock=True)
        if run["stage"] != "finalize":
            return
        matching_source(run, lock=True)
        if (
            run["next_part"] != run["part_count"]
            or run["verify_part"] != run["part_count"]
            or run["source_digest"] != run["copy_digest"]
        ):
            raise RuntimeError("resume copy is incomplete")
        if run["field"] == "text":
            saved = db.query_one(
                "SELECT count(*) AS parts, COALESCE(sum(length(text)), 0) AS total "
                "FROM resume_text_parts WHERE version_id = %s",
                (run_id,),
            )
            if saved != {"parts": run["part_count"], "total": run["total"]}:
                raise RuntimeError("resume text copy is incomplete")
            db.execute(
                "UPDATE resume_text_versions SET chars = %s, part_count = %s, ready = TRUE "
                "WHERE id = %s",
                (run["total"], run["part_count"], run_id),
            )
        else:
            saved = db.query_one(
                "SELECT count(*) AS parts, COALESCE(sum(size), 0) AS total, "
                "count(*) FILTER (WHERE NOT verified) AS unverified "
                "FROM resume_upload_parts WHERE upload_id = %s",
                (run_id,),
            )
            if saved != {"parts": run["part_count"], "total": run["total"], "unverified": 0}:
                raise RuntimeError("resume PDF copy is incomplete")
            db.execute("UPDATE resume_uploads SET status = 'ready' WHERE id = %s", (run_id,))
        table, pointer = TABLES[run["source_kind"]], POINTERS[run["field"]]
        db.execute(f"UPDATE {table} SET {pointer} = %s WHERE id = %s", (run_id, run["source_id"]))
        db.execute("UPDATE resume_backfill_runs SET stage = 'done' WHERE id = %s", (run_id,))


def advance(run_id: uuid.UUID) -> None:
    run = run_row(run_id)
    if run["stage"] == "done":
        return
    matching_source(run)
    if run["stage"] == "copy":
        if run["next_part"] == run["part_count"]:
            with db.transaction():
                current = run_row(run_id, lock=True)
                if current["stage"] == "copy" and current["next_part"] == current["part_count"]:
                    db.execute(
                        "UPDATE resume_backfill_runs SET stage = 'verify' WHERE id = %s", (run_id,)
                    )
                    queue_step(run_id, "verify", 0)
        elif run["field"] == "text":
            copy_text(run_id, run)
        else:
            copy_pdf(run_id, run)
    elif run["stage"] == "verify":
        if run["verify_part"] == run["part_count"]:
            with db.transaction():
                current = run_row(run_id, lock=True)
                if current["stage"] == "verify" and current["verify_part"] == current["part_count"]:
                    db.execute(
                        "UPDATE resume_backfill_runs SET stage = 'finalize' WHERE id = %s",
                        (run_id,),
                    )
                    queue_step(run_id, "finalize", 0)
        else:
            verify_part(run_id, run)
    elif run["stage"] == "finalize":
        finalize(run_id)
    else:
        raise RuntimeError("invalid resume backfill stage")


async def handle_backfill_resumes(task_id: int, payload: dict[str, Any]) -> None:
    if os.environ.get("JOBTRACKER_HOST_NAME") != "hetzner":
        raise RuntimeError("resume backfill must run on Hetzner")
    if not db.get_config("resume_backfill_enabled"):
        return
    await asyncio.to_thread(advance, uuid.UUID(payload["run_id"]))
