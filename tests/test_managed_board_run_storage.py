"""Where a managed-board run keeps its candidate snapshot, how the board's
runs are found, and how legacy inline snapshots move to objects and back."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from api import db
from api import managed_board_runs as runs
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
from tasks import managed_boards as managed_task
from tasks.runtime import payload_recovery
from tests.factories import ObjectClient
from tests.test_managed_board_execution import _admissible, _board_and_job


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    return store


def _payload(task_id: int) -> dict:
    return db.query_one("SELECT payload FROM tasks WHERE id = %s", (task_id,))["payload"]


def test_admission_stores_candidates_as_a_verified_object_and_nothing_inline(
    client, admin_headers, f, monkeypatch, objects
):
    _admissible(monkeypatch)
    board, job_id, url = _board_and_job(client, admin_headers, f)

    response = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)

    assert response.status_code == 200, response.text
    payload = _payload(response.json()["task_id"])
    assert "jobs" not in payload
    assert payload["candidate_count"] == 1
    stored = objects.get(PayloadRef.parse(payload["jobs_ref"]))
    assert [job["id"] for job in stored] == [job_id]
    assert stored[0]["url"] == url and "content" not in stored[0]
    assert runs.run_jobs(payload, objects) == stored


def test_a_title_the_review_gate_screens_never_enters_the_run(
    client, admin_headers, f, monkeypatch, objects, set_config
):
    _admissible(monkeypatch)
    board, job_id, _url = _board_and_job(client, admin_headers, f)
    f.make_ready_job(source="managed-source", title="Registered Nurse")
    set_config("title_screens", {board["prompt_hash"]: "nontechnical_occupations_v1"})

    response = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)

    payload = _payload(response.json()["task_id"])
    assert [job["id"] for job in runs.run_jobs(payload, objects)] == [job_id]
    assert payload["title_screens"] == ["nontechnical_occupations_v1"]


def test_storage_outage_at_admission_queues_nothing(client, admin_headers, f, monkeypatch, objects):
    _admissible(monkeypatch)
    board, _job_id, _url = _board_and_job(client, admin_headers, f)
    objects.client.fail_put = True

    response = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "STORAGE_UNAVAILABLE"
    assert db.query_one("SELECT count(*) AS n FROM tasks")["n"] == 0
    assert objects.client.objects == {}


@pytest.mark.asyncio
async def test_handler_runs_and_resumes_from_the_stored_candidates(
    client, admin_headers, f, monkeypatch, objects
):
    _admissible(monkeypatch)
    board, job_id, _url = _board_and_job(client, admin_headers, f)
    task_id = client.post(
        f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers
    ).json()["task_id"]
    db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (task_id,))
    assert "jobs" not in _payload(task_id)
    seen = []

    async def fake_batch(tid, cfg, snapshot, jobs, hooks, **kwargs):
        seen.append([job["id"] for job in jobs])
        hooks.complete()

    monkeypatch.setattr(managed_task, "execute_batch", fake_batch)
    # The first run and a resume both read the payload as the worker's claim
    # returns it, which carries only the reference.
    await managed_task.handle_run_managed_board_batch(task_id, _payload(task_id))
    await managed_task.handle_run_managed_board_batch(task_id, _payload(task_id))

    assert seen == [[job_id], [job_id]]


@pytest.mark.asyncio
async def test_unreadable_candidates_take_the_payload_recovery_path(
    client, admin_headers, f, monkeypatch, objects
):
    _admissible(monkeypatch)
    board, _job_id, _url = _board_and_job(client, admin_headers, f)
    task_id = client.post(
        f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers
    ).json()["task_id"]
    objects.client.fail_get = True
    monkeypatch.setattr(
        managed_task,
        "execute_batch",
        lambda *a, **kw: pytest.fail("nothing is submitted without the candidates"),
    )

    with pytest.raises(PayloadUnavailable):
        await managed_task.handle_run_managed_board_batch(task_id, _payload(task_id))

    db.execute(
        "UPDATE tasks SET status = 'failed', payload = payload || "
        '\'{"payload_recovery":{"reason":"payload_unavailable"}}\' WHERE id = %s',
        (task_id,),
    )
    assert payload_recovery.retry(task_id, objects) == "unavailable"
    objects.client.fail_get = False
    assert payload_recovery.retry(task_id, objects) == "pending"


def test_latest_and_active_check_answer_from_the_board_index(f):
    def run(kind, board, revision, status):
        return f.make_task(
            kind,
            {
                "managed_board_id": board,
                "revision": revision,
                "requested_model": "m",
                "reserved_tokens": 5,
            },
            status=status,
        )

    other = run("run_managed_board_batch", 2, 1, "done")
    first = run("run_managed_board", 1, 2, "done")
    second = run("run_managed_board_batch", 1, 3, "failed")
    run("run_filter_chunk", 1, 4, "done")

    run = runs.latest(1)
    assert run is not None and run.id == second and run.snapshot_revision == 3
    assert runs.latest(2).id == other and first < second
    assert runs.latest(3) is None

    # The rest of the queue, so the plan is chosen at a realistic ratio of
    # runs to everything else (production holds about 220k tasks).
    db.execute(
        "INSERT INTO tasks (kind, payload, status) SELECT 'ingest_source', "
        "jsonb_build_object('source', 's' || g), 'done' FROM generate_series(1, 5000) g"
    )
    db.execute("ANALYZE tasks")
    with db.transaction():
        # A prepared statement settles on a generic plan, which cannot prove a
        # partial index's predicate from a bound kind array; force one.
        db.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        db.execute("SET LOCAL enable_seqscan = off")
        for name, sql, types, args in (
            ("latest_probe", runs.LATEST_SQL, "bigint", "1"),
            ("active_probe", runs.ACTIVE_SQL, "bigint, text[]", "1, '{pending,running}'"),
        ):
            numbered = sql
            for number in range(1, sql.count("%s") + 1):
                numbered = numbered.replace("%s", f"${number}", 1)
            db.execute(f"PREPARE {name}({types}) AS {numbered}")
            plan = "\n".join(
                next(iter(row.values())) for row in db.query(f"EXPLAIN EXECUTE {name}({args})")
            )
            db.execute(f"DEALLOCATE {name}")
            assert "idx_tasks_managed_board" in plan, plan


JOBS = [{"id": 1, "url": "u1"}, {"id": 2, "url": "u2"}]


def test_a_run_with_neither_shape_is_reported_unavailable_not_empty():
    with pytest.raises(PayloadUnavailable):
        runs.run_jobs({"candidate_count": 2})


def test_reference_with_wrong_count_is_refused(objects):
    ref = asdict(objects.put_verified(JOBS))
    with pytest.raises(PayloadUnavailable):
        runs.run_jobs({"jobs_ref": ref, "candidate_count": 3}, objects)
