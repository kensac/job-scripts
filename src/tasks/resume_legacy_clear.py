"""Verify one canonical resume part before clearing its old copy."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from api import db, queue
from api.resumes.backfill_schedule import POINTERS, TABLES
from api.resumes.legacy_clear_schedule import snapshot
from api.resumes.text_versions import TEXT_PART_CHARS
from core.payloads.blob_parts import PART_BYTES, BlobPart, BlobPartStore
from core.payloads.objects import PayloadUnavailable
from tasks.resume_backfill import fold


def run_row(run_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    run = db.query_one(
        "SELECT source_kind, source_id, field, source_xmin, canonical_id, "
        "total, part_count, next_part, verified_digest, stage "
        "FROM resume_legacy_clear_runs WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (run_id,),
    )
    if run is None or run["source_kind"] not in TABLES or run["field"] not in POINTERS:
        raise RuntimeError("invalid resume clear run")
    return run


def matching_source(run: dict[str, Any], *, lock: bool = False) -> None:
    old = snapshot(run["source_kind"], run["source_id"], run["field"], lock=lock)
    if (
        old is None
        or old["source_xmin"] != run["source_xmin"]
        or old["canonical_id"] != run["canonical_id"]
        or old["total"] != run["total"]
    ):
        raise RuntimeError("old resume value changed during verification")


def source_part(run: dict[str, Any], index: int) -> bytes:
    field = run["field"]
    size = TEXT_PART_CHARS if field == "text" else PART_BYTES
    table = TABLES[run["source_kind"]]
    row = db.query_one(
        f"SELECT substring({field} from %s for %s) AS value FROM {table} "
        "WHERE id = %s AND xmin::text = %s",
        (index * size + 1, size, run["source_id"], run["source_xmin"]),
    )
    if row is None or row["value"] is None:
        raise RuntimeError("old resume value changed during verification")
    return row["value"].encode("utf-8") if field == "text" else bytes(row["value"])


def canonical_metadata(run: dict[str, Any]) -> None:
    if run["field"] == "text":
        row = db.query_one(
            "SELECT chars, part_count, ready FROM resume_text_versions WHERE id = %s",
            (run["canonical_id"],),
        )
        if row != {"chars": run["total"], "part_count": run["part_count"], "ready": True}:
            raise RuntimeError("canonical resume text is incomplete")
    else:
        row = db.query_one(
            "SELECT size, status FROM resume_uploads WHERE id = %s",
            (run["canonical_id"],),
        )
        if row != {"size": run["total"], "status": "ready"}:
            raise RuntimeError("canonical resume PDF is incomplete")


def pdf_part(run: dict[str, Any], index: int) -> tuple[dict[str, Any], bytes]:
    if not db.get_config("resume_pdf_uploads_enabled"):
        raise RuntimeError("PDF object storage is paused")
    receipt = db.query_one(
        "SELECT sha256, size, verified FROM resume_upload_parts "
        "WHERE upload_id = %s AND part_index = %s",
        (run["canonical_id"], index),
    )
    if receipt is None or not receipt["verified"]:
        raise RuntimeError("canonical resume PDF part is missing")
    part = BlobPart(run["canonical_id"], index, receipt["sha256"], receipt["size"])
    return receipt, BlobPartStore.from_env().read(part)


def queue_step(run_id: uuid.UUID, stage: str, index: int) -> None:
    queue.enqueue(
        "clear_resume_legacy",
        {"run_id": str(run_id)},
        dedupe_key=f"resume-legacy-clear:{run_id}:{stage}:{index}",
    )


def verify_part(run_id: uuid.UUID, run: dict[str, Any]) -> None:
    index = run["next_part"]
    receipt, object_bytes = pdf_part(run, index) if run["field"] == "pdf" else (None, None)
    with db.transaction():
        current = run_row(run_id, lock=True)
        if current["stage"] != "verify" or current["next_part"] != index:
            return
        matching_source(current, lock=True)
        canonical_metadata(current)
        original = source_part(current, index)
        expected_size = min(
            TEXT_PART_CHARS if current["field"] == "text" else PART_BYTES,
            current["total"]
            - index * (TEXT_PART_CHARS if current["field"] == "text" else PART_BYTES),
        )
        if current["field"] == "text":
            saved = db.query_one(
                "SELECT text FROM resume_text_parts WHERE version_id = %s AND part_index = %s",
                (current["canonical_id"], index),
            )
            if saved is None or len(saved["text"]) != expected_size:
                raise RuntimeError("canonical resume text part is missing")
            canonical = saved["text"].encode("utf-8")
        else:
            saved = db.query_one(
                "SELECT sha256, size, verified FROM resume_upload_parts "
                "WHERE upload_id = %s AND part_index = %s",
                (current["canonical_id"], index),
            )
            if saved is None or saved != receipt or saved["size"] != expected_size:
                raise RuntimeError("canonical resume PDF receipt changed")
            assert object_bytes is not None
            canonical = object_bytes
        if original != canonical:
            raise RuntimeError("canonical resume differs from old value")
        following = index + 1
        stage = "finalize" if following == current["part_count"] else "verify"
        db.execute(
            "UPDATE resume_legacy_clear_runs SET next_part = %s, verified_digest = %s, "
            "stage = %s, failed_at = NULL, last_error = NULL WHERE id = %s",
            (following, fold(current["verified_digest"], index, original), stage, run_id),
        )
        queue_step(run_id, stage, 0 if stage == "finalize" else following)


def finalize(run_id: uuid.UUID) -> None:
    with db.transaction():
        run = run_row(run_id, lock=True)
        if run["stage"] != "finalize":
            return
        matching_source(run, lock=True)
        canonical_metadata(run)
        if run["next_part"] != run["part_count"]:
            raise RuntimeError("canonical resume verification is incomplete")
        if run["field"] == "text":
            saved = db.query_one(
                "SELECT count(*) AS parts, COALESCE(sum(length(text)), 0) AS total "
                "FROM resume_text_parts WHERE version_id = %s",
                (run["canonical_id"],),
            )
        else:
            saved = db.query_one(
                "SELECT count(*) AS parts, COALESCE(sum(size), 0) AS total, "
                "count(*) FILTER (WHERE NOT verified) AS unverified "
                "FROM resume_upload_parts WHERE upload_id = %s",
                (run["canonical_id"],),
            )
            if saved is not None and saved["unverified"]:
                raise RuntimeError("canonical resume PDF has unverified parts")
        if saved is None or saved["parts"] != run["part_count"] or saved["total"] != run["total"]:
            raise RuntimeError("canonical resume parts are incomplete")
        table, field = TABLES[run["source_kind"]], run["field"]
        db.execute(f"UPDATE {table} SET {field} = NULL WHERE id = %s", (run["source_id"],))
        db.execute(
            "UPDATE resume_legacy_clear_runs SET stage = 'done', failed_at = NULL, "
            "last_error = NULL WHERE id = %s",
            (run_id,),
        )


def advance(run_id: uuid.UUID) -> None:
    run = run_row(run_id)
    if run["stage"] == "done":
        return
    if run["stage"] == "verify":
        if run["next_part"] < run["part_count"]:
            verify_part(run_id, run)
        else:
            with db.transaction():
                current = run_row(run_id, lock=True)
                if current["stage"] == "verify" and current["next_part"] == current["part_count"]:
                    db.execute(
                        "UPDATE resume_legacy_clear_runs SET stage = 'finalize' WHERE id = %s",
                        (run_id,),
                    )
                    queue_step(run_id, "finalize", 0)
    elif run["stage"] == "finalize":
        finalize(run_id)
    else:
        raise RuntimeError("invalid resume clear stage")


def process_run(run_id: uuid.UUID) -> None:
    try:
        advance(run_id)
    except (PayloadUnavailable, RuntimeError, ValueError) as exc:
        db.execute(
            "UPDATE resume_legacy_clear_runs SET failed_at = now(), last_error = %s WHERE id = %s",
            (str(exc), run_id),
        )


async def handle_clear_resume_legacy(task_id: int, payload: dict[str, Any]) -> None:
    if os.environ.get("JOBTRACKER_HOST_NAME") != "hetzner":
        raise RuntimeError("resume clearing must run on Hetzner")
    if not db.get_config("resume_legacy_clear_enabled"):
        return
    await asyncio.to_thread(process_run, uuid.UUID(payload["run_id"]))
