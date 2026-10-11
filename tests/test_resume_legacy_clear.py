"""Old resume fields clear only after exact canonical verification."""

from __future__ import annotations

import io
import uuid
from typing import cast

from api import db
from api.resumes import legacy_clear_schedule
from api.resumes.text_versions import store_text
from api.worker_lifecycle import claims
from core.payloads.blob_parts import PART_BYTES, BlobPartStore
from tasks import resume_legacy_clear
from tests.job_board_support import _uid


class MemoryObjects:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        self.objects[str(kwargs["Key"])] = bytes(cast(bytes, kwargs["Body"]))

    def get_object(self, **kwargs: object) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[str(kwargs["Key"])])}


def _enable() -> None:
    for key in ("resume_legacy_clear_enabled", "resume_pdf_uploads_enabled"):
        db.execute(
            "INSERT INTO app_config (key, value) VALUES (%s, 'true') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key,),
        )


def _canonical(
    user_id: int, text: str, pdf: bytes, store: BlobPartStore
) -> tuple[uuid.UUID, uuid.UUID]:
    version_id, _ = store_text(user_id, [text], allow_blank=True)
    upload_id = uuid.uuid4()
    db.execute(
        "INSERT INTO resume_uploads (id, user_id, name, filename, size, status) "
        "VALUES (%s, %s, 'resume', 'resume.pdf', %s, 'ready')",
        (upload_id, user_id, len(pdf)),
    )
    for index, offset in enumerate(range(0, len(pdf), PART_BYTES)):
        part = store.put(upload_id, index, pdf[offset : offset + PART_BYTES])
        db.execute(
            "INSERT INTO resume_upload_parts (upload_id, part_index, sha256, size, verified) "
            "VALUES (%s, %s, %s, %s, TRUE)",
            (upload_id, index, part.sha256, part.size),
        )
    return version_id, upload_id


def _run_one_value() -> uuid.UUID:
    legacy_clear_schedule.schedule_next(str(uuid.uuid4()))
    first_run = None
    while True:
        task = db.query_one(
            "SELECT id, payload FROM tasks WHERE kind = 'clear_resume_legacy' "
            "AND status = 'pending' ORDER BY id LIMIT 1"
        )
        if task is None:
            break
        run_id = uuid.UUID(task["payload"]["run_id"])
        first_run = first_run or run_id
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
        resume_legacy_clear.process_run(run_id)
    assert first_run is not None
    return first_run


def test_current_and_revision_text_and_pdf_clear_after_exact_copy(
    user_headers, monkeypatch
) -> None:
    user_id = _uid(user_headers)
    text = "A" * (resume_legacy_clear.TEXT_PART_CHARS + 7)
    pdf = b"%PDF-old" + b"x" * PART_BYTES
    objects = MemoryObjects()
    store = BlobPartStore(objects, "test")
    monkeypatch.setattr(BlobPartStore, "from_env", classmethod(lambda cls: store))
    version_id, upload_id = _canonical(user_id, text, pdf, store)
    current = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text, pdf, text_version_id, pdf_upload_id) "
        "VALUES (%s, 'current', %s, %s, %s, %s) RETURNING id",
        (user_id, text, pdf, version_id, upload_id),
    )
    assert current is not None
    revision = db.query_one(
        "INSERT INTO user_resume_revisions "
        "(user_id, resume_id, name, text, pdf, text_version_id, pdf_upload_id) "
        "VALUES (%s, %s, 'older', %s, %s, %s, %s) RETURNING id",
        (user_id, current["id"], text, pdf, version_id, upload_id),
    )
    assert revision is not None
    _enable()

    legacy_clear_schedule.schedule_next("first")
    first_task = db.query_one(
        "SELECT id, payload FROM tasks WHERE kind = 'clear_resume_legacy' ORDER BY id LIMIT 1"
    )
    assert first_task is not None
    first_run = uuid.UUID(first_task["payload"]["run_id"])
    resume_legacy_clear.advance(first_run)
    assert db.query_one(
        "SELECT next_part, stage FROM resume_legacy_clear_runs WHERE id = %s", (first_run,)
    ) == {"next_part": 1, "stage": "verify"}
    assert db.query_one("SELECT text FROM user_resumes WHERE id = %s", (current["id"],)) == {
        "text": text
    }
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (first_task["id"],))
    while db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'clear_resume_legacy' AND status = 'pending'"
    ):
        task = db.query_one(
            "SELECT id, payload FROM tasks WHERE kind = 'clear_resume_legacy' "
            "AND status = 'pending' ORDER BY id LIMIT 1"
        )
        assert task is not None
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
        resume_legacy_clear.advance(uuid.UUID(task["payload"]["run_id"]))
    for _ in range(3):
        _run_one_value()

    for table, row_id in (
        ("user_resumes", current["id"]),
        ("user_resume_revisions", revision["id"]),
    ):
        assert db.query_one(
            f"SELECT text, pdf, text_version_id, pdf_upload_id FROM {table} WHERE id = %s",
            (row_id,),
        ) == {"text": None, "pdf": None, "text_version_id": version_id, "pdf_upload_id": upload_id}
    assert db.query_one(
        "SELECT count(*) AS n FROM resume_legacy_clear_runs WHERE stage = 'done'"
    ) == {"n": 4}


def test_missing_pointer_blocks_clearing(user_headers) -> None:
    user_id = _uid(user_headers)
    db.execute(
        "INSERT INTO user_resumes (user_id, name, text) VALUES (%s, 'old', 'text')", (user_id,)
    )
    _enable()
    legacy_clear_schedule.schedule_next("blocked")
    assert db.query_one("SELECT 1 FROM tasks WHERE kind = 'clear_resume_legacy'") is None


def test_mismatch_keeps_old_value_and_later_row_runs(user_headers, monkeypatch) -> None:
    user_id = _uid(user_headers)
    wrong_version, _ = store_text(user_id, ["wrong"], allow_blank=True)
    good_version, _ = store_text(user_id, ["good"], allow_blank=True)
    first = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text, text_version_id) "
        "VALUES (%s, 'first', 'old', %s) RETURNING id",
        (user_id, wrong_version),
    )
    second = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text, text_version_id) "
        "VALUES (%s, 'second', 'good', %s) RETURNING id",
        (user_id, good_version),
    )
    assert first is not None and second is not None
    _enable()
    legacy_clear_schedule.schedule_next("first")
    task = db.query_one(
        "SELECT id, payload FROM tasks WHERE kind = 'clear_resume_legacy' ORDER BY id LIMIT 1"
    )
    assert task is not None
    first_run = uuid.UUID(task["payload"]["run_id"])
    resume_legacy_clear.process_run(first_run)
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
    assert db.query_one("SELECT text FROM user_resumes WHERE id = %s", (first["id"],)) == {
        "text": "old"
    }
    assert db.query_one(
        "SELECT failed_at IS NOT NULL AS failed FROM resume_legacy_clear_runs WHERE id = %s",
        (first_run,),
    ) == {"failed": True}
    legacy_clear_schedule.schedule_next("second")
    pending = db.query_one(
        "SELECT payload FROM tasks WHERE kind = 'clear_resume_legacy' "
        "AND status = 'pending' ORDER BY id LIMIT 1"
    )
    assert pending is not None
    assert (
        resume_legacy_clear.run_row(uuid.UUID(pending["payload"]["run_id"]))["source_id"]
        == second["id"]
    )


def test_claim_requires_hetzner(monkeypatch) -> None:
    resume_legacy_clear.queue.enqueue("clear_resume_legacy", {"run_id": str(uuid.uuid4())})
    monkeypatch.delenv("JOBTRACKER_HOST_NAME", raising=False)
    assert claims.claim_task("other") is None
    monkeypatch.setenv("JOBTRACKER_HOST_NAME", "hetzner")
    assert claims.claim_task("hetzner") is not None


def test_changed_source_cannot_clear_old_text(user_headers) -> None:
    user_id = _uid(user_headers)
    old_text = "A" * (resume_legacy_clear.TEXT_PART_CHARS + 1)
    version_id, _ = store_text(user_id, [old_text], allow_blank=True)
    row = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text, text_version_id) "
        "VALUES (%s, 'current', %s, %s) RETURNING id",
        (user_id, old_text, version_id),
    )
    assert row is not None
    _enable()
    legacy_clear_schedule.schedule_next("first")
    task = db.query_one(
        "SELECT payload FROM tasks WHERE kind = 'clear_resume_legacy' ORDER BY id LIMIT 1"
    )
    assert task is not None
    run_id = uuid.UUID(task["payload"]["run_id"])
    resume_legacy_clear.advance(run_id)
    db.execute("UPDATE user_resumes SET name = 'renamed' WHERE id = %s", (row["id"],))
    resume_legacy_clear.process_run(run_id)
    assert db.query_one("SELECT text FROM user_resumes WHERE id = %s", (row["id"],)) == {
        "text": old_text
    }
    assert db.query_one(
        "SELECT failed_at IS NOT NULL AS failed FROM resume_legacy_clear_runs WHERE id = %s",
        (run_id,),
    ) == {"failed": True}


def test_unavailable_pdf_copy_keeps_old_bytes(user_headers, monkeypatch) -> None:
    user_id = _uid(user_headers)
    old_pdf = b"%PDF-stored"
    objects = MemoryObjects()
    store = BlobPartStore(objects, "test")
    monkeypatch.setattr(BlobPartStore, "from_env", classmethod(lambda cls: store))
    _, upload_id = _canonical(user_id, "", old_pdf, store)
    row = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text, pdf, pdf_upload_id) "
        "VALUES (%s, 'current', NULL, %s, %s) RETURNING id",
        (user_id, old_pdf, upload_id),
    )
    assert row is not None
    objects.objects.clear()
    _enable()
    legacy_clear_schedule.schedule_next("pdf")
    task = db.query_one(
        "SELECT payload FROM tasks WHERE kind = 'clear_resume_legacy' ORDER BY id LIMIT 1"
    )
    assert task is not None
    run_id = uuid.UUID(task["payload"]["run_id"])
    resume_legacy_clear.process_run(run_id)
    assert db.query_one("SELECT pdf FROM user_resumes WHERE id = %s", (row["id"],)) == {
        "pdf": old_pdf
    }
    assert db.query_one(
        "SELECT failed_at IS NOT NULL AS failed FROM resume_legacy_clear_runs WHERE id = %s",
        (run_id,),
    ) == {"failed": True}
