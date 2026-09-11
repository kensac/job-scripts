from __future__ import annotations

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg import errors

from api import db, telemetry
from tasks import user_job_backfill
from tasks.runtime import TaskClaim, set_current_claim


def _task(f, *, cutoff: str = "2100-01-01T00:00:00Z", status: str = "running") -> int:
    return f.make_task(
        "backfill_user_job_split",
        {"version": 1, "generation": 1, "legacy_before": cutoff, "total": 1},
        status=status,
    )


def _legacy(f, **fields) -> tuple[int, int]:
    user_id, job_id = f.make_user(), f.make_job()
    columns = ["user_id", "job_id", *fields]
    values = [user_id, job_id, *fields.values()]
    db.execute(
        f"INSERT INTO user_jobs ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))})",
        values,
    )
    return user_id, job_id


def _eligible_worker(name="split-worker", *, kinds=None, excluded=None, fresh=True, release=None):
    db.execute(
        "INSERT INTO worker_status "
        "(name, started_at, last_seen, kinds, excluded_kinds, release) "
        "VALUES (%s, now(), now() - %s::interval, %s, %s, %s)",
        (
            name,
            "0 seconds" if fresh else "3 minutes",
            kinds or [],
            excluded or [],
            telemetry.RELEASE if release is None else release,
        ),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "Interested"),
        ("date_applied", datetime.date(2026, 1, 2)),
        ("notes", "note"),
        ("size", "large"),
        ("recruiter", "Alex"),
        ("connection1", "Blair"),
        ("connection2", "Casey"),
        ("documents", "resume"),
        ("hidden", True),
    ],
)
@pytest.mark.asyncio
async def test_every_person_field_marks_the_legacy_row(f, field, value):
    user_id, job_id = _legacy(f, **{field: value})
    db.execute(
        "UPDATE user_jobs SET created_at = '2026-01-01T00:00:00Z', "
        "updated_at = '2026-02-03T04:05:06Z' WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )
    task_id = _task(f)

    await user_job_backfill.handle_backfill_user_job_split(task_id, {})

    row = db.query_one(
        "SELECT person_touched_at, updated_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )
    assert row["person_touched_at"] == row["updated_at"]
    assert db.query_one("SELECT 1 FROM user_job_working_set") is None


@pytest.mark.asyncio
async def test_partition_keeps_known_people_and_names_unknown_separately(f):
    touched_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    known_user, known_job = _legacy(f, person_touched_at=touched_at)
    unknown_user, unknown_job = _legacy(f)
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)",
        (unknown_user, unknown_job),
    )
    task_id = _task(f)

    await user_job_backfill.handle_backfill_user_job_split(task_id, {})

    assert (
        db.query_one(
            "SELECT person_touched_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (known_user, known_job),
        )["person_touched_at"]
        == touched_at
    )
    assert (
        db.query_one(
            "SELECT person_touched_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (unknown_user, unknown_job),
        )["person_touched_at"]
        is None
    )
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["already_touched"] == 1
    assert progress["legacy_unknown"] == 1
    assert progress["working_set_inserted"] == 0


@pytest.mark.asyncio
async def test_empty_string_defaults_remain_legacy_unknown(f):
    user_id, job_id = _legacy(
        f,
        status="",
        notes="",
        size="",
        recruiter="",
        connection1="",
        connection2="",
        documents="",
        hidden=False,
    )
    task_id = _task(f)

    await user_job_backfill.handle_backfill_user_job_split(task_id, {})

    assert (
        db.query_one(
            "SELECT person_touched_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )["person_touched_at"]
        is None
    )
    assert db.query_one(
        "SELECT 1 FROM user_job_working_set WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )


@pytest.mark.asyncio
async def test_cutoff_and_cancellation_leave_rows_unclassified(f):
    user_id, job_id = _legacy(f)
    cancelled = _task(f, status="cancelled")
    await user_job_backfill.handle_backfill_user_job_split(cancelled, {})
    cutoff_task = _task(f, cutoff="2000-01-01T00:00:00Z")
    await user_job_backfill.handle_backfill_user_job_split(cutoff_task, {})
    assert (
        db.query_one(
            "SELECT person_touched_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )["person_touched_at"]
        is None
    )
    assert db.query_one("SELECT 1 FROM user_job_working_set") is None


@pytest.mark.asyncio
async def test_lost_claim_writes_nothing(f):
    _legacy(f, notes="person")
    task_id = _task(f)
    db.execute("UPDATE tasks SET worker = 'new', attempts = 2 WHERE id = %s", (task_id,))
    set_current_claim(TaskClaim(task_id, "old", 1))
    try:
        await user_job_backfill.handle_backfill_user_job_split(task_id, {})
    finally:
        set_current_claim(None)
    assert db.query_one("SELECT person_touched_at FROM user_jobs")["person_touched_at"] is None


@pytest.mark.asyncio
async def test_failed_domain_write_rolls_back_checkpoint_and_person_mark(f):
    _legacy(f, notes="person")
    _legacy(f)
    task_id = _task(f)
    before = db.query_one("SELECT payload, progress FROM tasks WHERE id = %s", (task_id,))
    db.execute(
        "CREATE FUNCTION test_refuse_split_insert() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'refuse split insert'; END $$"
    )
    db.execute(
        "CREATE TRIGGER test_refuse_split_insert BEFORE INSERT ON user_job_working_set "
        "FOR EACH ROW EXECUTE FUNCTION test_refuse_split_insert()"
    )
    try:
        with pytest.raises(errors.RaiseException, match="refuse split insert"):
            await user_job_backfill.handle_backfill_user_job_split(task_id, {})
    finally:
        db.execute("DROP TRIGGER test_refuse_split_insert ON user_job_working_set")
        db.execute("DROP FUNCTION test_refuse_split_insert()")
    assert (
        db.query_one("SELECT count(*) AS n FROM user_jobs WHERE person_touched_at IS NOT NULL")["n"]
        == 0
    )
    assert db.query_one("SELECT payload, progress FROM tasks WHERE id = %s", (task_id,)) == before


@pytest.mark.asyncio
async def test_fixed_batches_checkpoint_and_resume_after_five_hundred_rows(f):
    user_id = f.make_user()
    db.execute(
        """
        WITH inserted AS (
            INSERT INTO jobs (url, raw_url, source)
            SELECT 'https://backfill.test/' || n, 'https://backfill.test/' || n, 'test'
            FROM generate_series(1, 501) n RETURNING id
        )
        INSERT INTO user_jobs (user_id, job_id) SELECT %s, id FROM inserted
        """,
        (user_id,),
    )
    task_id = f.make_task(
        "backfill_user_job_split",
        {"version": 1, "generation": 1, "legacy_before": "2100-01-01T00:00:00Z", "total": 501},
        status="running",
    )

    counts, finished = user_job_backfill._process_batch(task_id)
    assert finished is False and counts["legacy_unknown"] == 500
    task = db.query_one("SELECT payload FROM tasks WHERE id = %s", (task_id,))
    assert task["payload"][user_job_backfill.CHECKPOINT_KEY]["job_id"] > 0

    await user_job_backfill.handle_backfill_user_job_split(task_id, {})

    assert db.query_one("SELECT count(*) AS n FROM user_job_working_set")["n"] == 501
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["legacy_unknown"] == 501


@pytest.mark.asyncio
async def test_zero_rows_reports_zero_counts(f):
    task_id = f.make_task(
        "backfill_user_job_split",
        {"version": 1, "generation": 1, "legacy_before": "2100-01-01T00:00:00Z", "total": 0},
        status="running",
    )
    await user_job_backfill.handle_backfill_user_job_split(task_id, {})
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress == {
        "done": 0,
        "total": 0,
        "label": "legacy job split complete",
        "already_touched": 0,
        "person_marked": 0,
        "legacy_unknown": 0,
        "working_set_inserted": 0,
    }


def test_same_checkpoint_refreshes_heartbeat_without_claiming_progress(f):
    task_id = _task(f)
    user_job_backfill._process_batch(task_id)
    before = db.query_one("SELECT progress_at, last_heartbeat FROM tasks WHERE id = %s", (task_id,))
    assert before is not None and before["progress_at"] is not None
    db.query_one("SELECT pg_sleep(0.01)")

    user_job_backfill._process_batch(task_id)

    after = db.query_one("SELECT progress_at, last_heartbeat FROM tasks WHERE id = %s", (task_id,))
    assert after is not None
    assert after["progress_at"] == before["progress_at"]
    assert after["last_heartbeat"] > before["last_heartbeat"]


@pytest.mark.asyncio
async def test_checkpoint_event_is_published_after_domain_commit(f, monkeypatch):
    user_id, job_id = _legacy(f)
    task_id = _task(f)
    observed = []

    def observe(published_task_id):
        observed.append(
            (
                published_task_id,
                db.query_one("SELECT payload FROM tasks WHERE id = %s", (published_task_id,))[
                    "payload"
                ],
                db.query_one(
                    "SELECT 1 FROM user_job_working_set WHERE user_id = %s AND job_id = %s",
                    (user_id, job_id),
                ),
            )
        )

    monkeypatch.setattr(user_job_backfill.events, "publish_task", observe)

    await user_job_backfill.handle_backfill_user_job_split(task_id, {})

    assert len(observed) == 1
    assert observed[0][0] == task_id
    assert user_job_backfill.CHECKPOINT_KEY in observed[0][1]
    assert observed[0][2] == {"?column?": 1}


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_admin_admission_dedupes_and_continues_only_after_terminal_failure(
    client, admin_headers, terminal_status
):
    _eligible_worker()
    before = db.query_one("SELECT now() AS at")["at"]
    first = client.post("/v1/admin/tasks/backfill-user-job-split", headers=admin_headers)
    after = db.query_one("SELECT now() AS at")["at"]
    assert first.status_code == 200, first.text
    same = client.post("/v1/admin/tasks/backfill-user-job-split", headers=admin_headers)
    assert same.json() == first.json()
    task_id = first.json()["task_id"]
    first_payload = db.query_one("SELECT payload FROM tasks WHERE id = %s", (task_id,))["payload"]
    cutoff = datetime.datetime.fromisoformat(first_payload["legacy_before"])
    assert before <= cutoff <= after
    checkpoint = {
        "user_id": 7,
        "job_id": 9,
        "already_touched": 1,
        "person_marked": 2,
        "legacy_unknown": 3,
        "working_set_inserted": 3,
    }
    db.execute(
        "UPDATE tasks SET status = %s, payload = jsonb_set(payload, %s, %s) WHERE id = %s",
        (terminal_status, [user_job_backfill.CHECKPOINT_KEY], db.jsonb(checkpoint), task_id),
    )
    continued = client.post("/v1/admin/tasks/backfill-user-job-split", headers=admin_headers)
    assert continued.json()["task_id"] != task_id
    payloads = db.query(
        "SELECT payload, dedupe_key FROM tasks WHERE kind = 'backfill_user_job_split' ORDER BY id"
    )
    assert [row["payload"]["generation"] for row in payloads] == [1, 2]
    assert len({row["dedupe_key"] for row in payloads}) == 2
    assert payloads[0]["payload"]["legacy_before"] == payloads[1]["payload"]["legacy_before"]
    assert payloads[1]["payload"][user_job_backfill.CHECKPOINT_KEY] == checkpoint
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (continued.json()["task_id"],))
    done = client.post("/v1/admin/tasks/backfill-user-job-split", headers=admin_headers)
    assert done.json() == {"task_id": continued.json()["task_id"], "status": "done"}


def test_concurrent_admin_admission_serializes_to_one_generation(client, admin_headers):
    _eligible_worker()
    assert client.get("/v1/admin/tasks", headers=admin_headers).status_code == 200
    barrier = threading.Barrier(4)

    def admit():
        barrier.wait()
        response = client.post("/v1/admin/tasks/backfill-user-job-split", headers=admin_headers)
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=4) as executor:
        admissions = list(executor.map(lambda _: admit(), range(4)))

    assert len({row["task_id"] for row in admissions}) == 1
    assert (
        db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'backfill_user_job_split'")["n"]
        == 1
    )


@pytest.mark.parametrize(
    "worker",
    [
        {"fresh": False},
        {"release": "different-release"},
        {"kinds": ["ingest_source"]},
        {"excluded": ["backfill_user_job_split"]},
    ],
)
def test_admin_admission_refuses_when_no_current_worker_is_eligible(client, admin_headers, worker):
    _eligible_worker(**worker)

    response = client.post("/v1/admin/tasks/backfill-user-job-split", headers=admin_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "NO_ELIGIBLE_WORKER"
    assert db.query_one("SELECT 1 FROM tasks WHERE kind = 'backfill_user_job_split'") is None
