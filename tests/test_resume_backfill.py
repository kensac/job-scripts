"""Resume backfill checkpoints one part and preserves every source value."""

from __future__ import annotations

import io
import uuid
from typing import cast

import pytest

from api import db
from api.resumes import backfill_schedule
from api.worker_lifecycle import claims
from core.payloads.blob_parts import PART_BYTES, BlobPart, BlobPartStore
from core.payloads.objects import PayloadUnavailable
from tasks import resume_backfill
from tests.job_board_support import _uid


class MemoryObjects:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        self.objects[str(kwargs["Key"])] = bytes(cast(bytes, kwargs["Body"]))

    def get_object(self, **kwargs: object) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[str(kwargs["Key"])])}


def _enable() -> None:
    for key in ("resume_backfill_enabled", "resume_pdf_uploads_enabled"):
        db.execute(
            "INSERT INTO app_config (key, value) VALUES (%s, 'true') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key,),
        )


def _drive_one_value() -> uuid.UUID:
    backfill_schedule.schedule_next(str(uuid.uuid4()))
    first_run_id = None
    while True:
        task = db.query_one(
            "SELECT id, payload FROM tasks WHERE kind = 'backfill_resumes' "
            "AND status = 'pending' ORDER BY id LIMIT 1"
        )
        if task is None:
            break
        run_id = uuid.UUID(task["payload"]["run_id"])
        first_run_id = first_run_id or run_id
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
        resume_backfill.advance(run_id)
    assert first_run_id is not None
    return first_run_id


def test_text_and_pdf_parts_checkpoint_and_keep_old_values(user_headers, monkeypatch) -> None:
    user_id = _uid(user_headers)
    old_text = "A" * (resume_backfill.TEXT_PART_CHARS + 9)
    old_pdf = b"%PDF-old" + b"x" * PART_BYTES
    current = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text, pdf) "
        "VALUES (%s, 'main', %s, %s) RETURNING id",
        (user_id, old_text, old_pdf),
    )
    assert current is not None
    db.execute(
        "INSERT INTO user_resume_revisions (user_id, resume_id, name, text, pdf) "
        "VALUES (%s, %s, 'older', '  ', %s)",
        (user_id, current["id"], old_pdf),
    )
    revision = db.query_one(
        "SELECT id FROM user_resume_revisions WHERE resume_id = %s", (current["id"],)
    )
    assert revision is not None
    objects = MemoryObjects()
    store = BlobPartStore(objects, "test")
    monkeypatch.setattr(BlobPartStore, "from_env", classmethod(lambda cls: store))
    _enable()

    backfill_schedule.schedule_next("first")
    started = db.query_one(
        "SELECT id, payload FROM tasks WHERE kind = 'backfill_resumes' ORDER BY id LIMIT 1"
    )
    assert started is not None
    first_run = uuid.UUID(started["payload"]["run_id"])
    resume_backfill.advance(first_run)
    progress = db.query_one(
        "SELECT next_part, stage FROM resume_backfill_runs WHERE id = %s", (first_run,)
    )
    assert progress == {"next_part": 1, "stage": "copy"}
    assert db.query_one(
        "SELECT text_version_id FROM user_resumes WHERE id = %s", (current["id"],)
    ) == {"text_version_id": None}
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (started["id"],))
    while db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'backfill_resumes' AND status = 'pending'"
    ):
        task = db.query_one(
            "SELECT id, payload FROM tasks WHERE kind = 'backfill_resumes' "
            "AND status = 'pending' ORDER BY id LIMIT 1"
        )
        assert task is not None
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
        resume_backfill.advance(uuid.UUID(task["payload"]["run_id"]))
    for _ in range(3):
        _drive_one_value()

    for kind, row_id, expected_text in (
        ("current", current["id"], old_text),
        ("revision", revision["id"], "  "),
    ):
        table = resume_backfill.TABLES[kind]
        saved = db.query_one(
            f"SELECT text, pdf, text_version_id, pdf_upload_id FROM {table} WHERE id = %s",
            (row_id,),
        )
        assert saved is not None
        assert saved["text"] == expected_text and saved["pdf"] == old_pdf
        parts = db.query(
            "SELECT text FROM resume_text_parts WHERE version_id = %s ORDER BY part_index",
            (saved["text_version_id"],),
        )
        assert "".join(part["text"] for part in parts) == expected_text
        pdf_parts = db.query(
            "SELECT part_index, sha256, size FROM resume_upload_parts "
            "WHERE upload_id = %s ORDER BY part_index",
            (saved["pdf_upload_id"],),
        )
        assert (
            b"".join(
                store.read(
                    BlobPart(saved["pdf_upload_id"], p["part_index"], p["sha256"], p["size"])
                )
                for p in pdf_parts
            )
            == old_pdf
        )


def test_claim_requires_hetzner(monkeypatch) -> None:
    task_id = resume_backfill.queue.enqueue("backfill_resumes", {"run_id": str(uuid.uuid4())})
    monkeypatch.delenv("JOBTRACKER_HOST_NAME", raising=False)
    assert claims.claim_task("other") is None
    monkeypatch.setenv("JOBTRACKER_HOST_NAME", "hetzner")
    claimed = claims.claim_task("hetzner")
    assert claimed is not None and claimed["id"] == task_id


def test_failed_pdf_part_keeps_old_bytes(user_headers, monkeypatch) -> None:
    user_id = _uid(user_headers)
    old = b"old PDF bytes"
    row = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text, pdf) "
        "VALUES (%s, 'main', '', %s) RETURNING id",
        (user_id, old),
    )
    assert row is not None
    objects = MemoryObjects()
    store = BlobPartStore(objects, "test")
    monkeypatch.setattr(BlobPartStore, "from_env", classmethod(lambda cls: store))
    monkeypatch.setattr(store, "read", lambda part: b"different bytes")
    _enable()
    backfill_schedule.schedule_next("text")
    while True:
        task = db.query_one(
            "SELECT id, payload FROM tasks WHERE kind = 'backfill_resumes' "
            "AND status = 'pending' ORDER BY id LIMIT 1"
        )
        assert task is not None
        run_id = uuid.UUID(task["payload"]["run_id"])
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
        resume_backfill.advance(run_id)
        if resume_backfill.run_row(run_id)["stage"] == "done":
            break
    backfill_schedule.schedule_next("pdf")
    task = db.query_one(
        "SELECT payload FROM tasks WHERE kind = 'backfill_resumes' "
        "AND status = 'pending' ORDER BY id LIMIT 1"
    )
    assert task is not None
    pdf_run = uuid.UUID(task["payload"]["run_id"])
    with pytest.raises(PayloadUnavailable, match="round-trip mismatch"):
        resume_backfill.advance(pdf_run)
    saved = db.query_one("SELECT pdf, pdf_upload_id FROM user_resumes WHERE id = %s", (row["id"],))
    assert saved == {"pdf": old, "pdf_upload_id": None}
    assert db.query_one("SELECT next_part FROM resume_backfill_runs WHERE id = %s", (pdf_run,)) == {
        "next_part": 0
    }


def test_source_change_refuses_old_checkpoint(user_headers) -> None:
    user_id = _uid(user_headers)
    original = "A" * (resume_backfill.TEXT_PART_CHARS + 1)
    row = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text) VALUES (%s, 'main', %s) RETURNING id",
        (user_id, original),
    )
    assert row is not None
    _enable()
    backfill_schedule.schedule_next("first")
    task = db.query_one(
        "SELECT payload FROM tasks WHERE kind = 'backfill_resumes' ORDER BY id LIMIT 1"
    )
    assert task is not None
    run_id = uuid.UUID(task["payload"]["run_id"])
    resume_backfill.advance(run_id)
    db.execute("UPDATE user_resumes SET name = 'renamed' WHERE id = %s", (row["id"],))
    with pytest.raises(RuntimeError, match="source changed"):
        resume_backfill.advance(run_id)
    assert db.query_one(
        "SELECT text, text_version_id FROM user_resumes WHERE id = %s", (row["id"],)
    ) == {"text": original, "text_version_id": None}


def test_failed_row_does_not_block_later_row(user_headers, monkeypatch) -> None:
    user_id = _uid(user_headers)
    first = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text) "
        "VALUES (%s, 'first', 'first text') RETURNING id",
        (user_id,),
    )
    second = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text) "
        "VALUES (%s, 'second', 'second text') RETURNING id",
        (user_id,),
    )
    assert first is not None and second is not None
    _enable()
    backfill_schedule.schedule_next("first")
    task = db.query_one(
        "SELECT id, payload FROM tasks WHERE kind = 'backfill_resumes' ORDER BY id LIMIT 1"
    )
    assert task is not None
    first_run = uuid.UUID(task["payload"]["run_id"])
    original = resume_backfill.source_part

    def fail_first(run: dict[str, object], index: int) -> str | bytes:
        if run["source_id"] == first["id"]:
            raise PayloadUnavailable("source part cannot be read")
        return original(run, index)

    monkeypatch.setattr(resume_backfill, "source_part", fail_first)
    resume_backfill.process_run(first_run)
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
    assert db.query_one(
        "SELECT failed_at IS NOT NULL AS failed FROM resume_backfill_runs WHERE id = %s",
        (first_run,),
    ) == {"failed": True}
    backfill_schedule.schedule_next("second")
    next_task = db.query_one(
        "SELECT payload FROM tasks WHERE kind = 'backfill_resumes' AND status = 'pending' "
        "ORDER BY id LIMIT 1"
    )
    assert next_task is not None
    second_run = uuid.UUID(next_task["payload"]["run_id"])
    assert resume_backfill.run_row(second_run)["source_id"] == second["id"]
