"""Where a managed-board run keeps its candidate snapshot, how the board's
runs are found, and how finished runs give their inline snapshots back."""

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

    # The internship board's shadow title gate keeps every candidate.
    assert seen == [[job_id], [job_id]]
    report = _payload(task_id)["title_gate_report"]
    assert report["candidate_count"] == 1


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


def _finished(f, *, status="done", days=30, **payload):
    task_id = f.make_task(
        payload.pop("kind", "run_managed_board_batch"),
        {"managed_board_id": 1, **payload},
        status=status,
    )
    db.execute(
        "UPDATE tasks SET finished_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, task_id),
    )
    return task_id


JOBS = [{"id": 1, "url": "u1"}, {"id": 2, "url": "u2"}]


def test_retention_strips_only_finished_unreferenced_inline_candidates(f):
    db.execute(
        "UPDATE app_config SET value = '7' WHERE key = 'managed_board_run_jobs_retention_days'"
    )
    eligible = [
        _finished(f, jobs=JOBS),
        _finished(f, status="cancelled", jobs=JOBS),
        _finished(f, status="failed", jobs=JOBS, batch_ids=[]),
        _finished(f, kind="run_managed_board", jobs=JOBS),
    ]
    kept = [
        _finished(f, jobs=JOBS, days=1),
        _finished(f, status="failed", jobs=JOBS, payload_recovery={"reason": "x"}),
        _finished(f, status="waiting", jobs=JOBS),
        _finished(f, jobs=JOBS, batch_ids=["b"]),
        _finished(f, kind="run_filter_chunk", jobs=JOBS),
        _finished(f, jobs_ref={"key": "k"}, candidate_count=2),
    ]
    receipt_owner = _finished(f, jobs=JOBS)
    kept.append(receipt_owner)
    db.execute(
        "INSERT INTO batch_result_receipts (provider_batch_id, custom_id, task_id, response) "
        "VALUES ('b', 'c', %s, '{}')",
        (receipt_owner,),
    )
    before = {row["id"]: row["payload"] for row in db.query("SELECT id, payload FROM tasks")}
    through = max(before)

    counted = runs.strip_finished_jobs(after=0, through=through, limit=100, dry_run=True)
    assert counted["eligible"] == len(eligible) and counted["stripped"] == 0
    assert {
        row["id"]: row["payload"] for row in db.query("SELECT id, payload FROM tasks")
    } == before

    # Three finished runs an invocation, resumed from each cursor.
    results, cursor = [], 0
    while not results or not results[-1]["exhausted"]:
        results.append(
            runs.strip_finished_jobs(after=cursor, through=through, limit=3, dry_run=False)
        )
        cursor = results[-1]["after"]
    assert all(result["scanned"] <= 3 for result in results) and len(results) > 1
    assert sum(result["stripped"] for result in results) == len(eligible)
    assert sum(result["bytes_before"] for result in results) > sum(
        result["bytes_after"] for result in results
    )

    after = {row["id"]: row["payload"] for row in db.query("SELECT id, payload FROM tasks")}
    for task_id in eligible:
        assert after[task_id] == {
            **{k: v for k, v in before[task_id].items() if k != "jobs"},
            "candidate_count": 2,
        }
    for task_id in kept:
        assert after[task_id] == before[task_id]

    again = runs.strip_finished_jobs(after=0, through=through, limit=100, dry_run=False)
    assert again["stripped"] == 0 and again["exhausted"]
    assert {row["id"]: row["payload"] for row in db.query("SELECT id, payload FROM tasks")} == after


def test_stripped_run_is_reported_unavailable_not_empty():
    with pytest.raises(PayloadUnavailable):
        runs.run_jobs({"candidate_count": 2})


def test_reference_with_wrong_count_is_refused(objects):
    ref = asdict(objects.put_verified(JOBS))
    with pytest.raises(PayloadUnavailable):
        runs.run_jobs({"jobs_ref": ref, "candidate_count": 3}, objects)
