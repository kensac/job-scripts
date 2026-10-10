"""Where a managed-board run keeps its candidate snapshot, how the board's
runs are found, and how legacy inline snapshots move to objects and back."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from api import db, task_jobs
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
    set_config(
        "filter_review_gate",
        {
            "title_mode": "enforce",
            "scopes": {board["prompt_hash"]: {"title_recipe": "nontechnical_occupations_v1"}},
        },
    )

    response = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)

    payload = _payload(response.json()["task_id"])
    assert [job["id"] for job in runs.run_jobs(payload, objects)] == [job_id]
    assert payload["title_recipe"] == "nontechnical_occupations_v1"


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


def _run(f, *, status="done", kind="run_managed_board_batch", **payload):
    return f.make_task(kind, {"managed_board_id": 1, "prompt": "p", **payload}, status=status)


def _texts() -> dict[int, str]:
    return {row["id"]: row["t"] for row in db.query("SELECT id, payload::text AS t FROM tasks")}


def _legacy(task_id: int) -> list:
    """Rewrite an admitted run into the inline shape admission wrote before
    the reference existed, and return its candidates."""
    jobs = runs.run_jobs(_payload(task_id))
    db.execute(
        "UPDATE tasks SET payload = (payload - 'jobs_ref' - 'candidate_count') || "
        "jsonb_build_object('jobs', %s::jsonb) WHERE id = %s",
        (db.jsonb(jobs), task_id),
    )
    return jobs


def _all(mode: str, objects, *, limit: int = 100, workers: int = 1) -> list[dict]:
    through = db.query_one("SELECT max(id) AS n FROM tasks")["n"]
    results, cursor = [], 0
    while not results or not results[-1]["exhausted"]:
        results.append(
            task_jobs.migrate(
                task_jobs.MANAGED_BOARD_RUNS,
                mode,
                after=cursor,
                through=through,
                limit=limit,
                store=objects,
                workers=workers,
            )
        )
        cursor = results[-1]["after"]
    return results


def _count(results: list[dict], outcome: str) -> int:
    return sum(result["counts"].get(outcome, 0) for result in results)


JOBS = [{"id": 1, "url": "u1"}, {"id": 2, "url": "u2"}]


@pytest.mark.asyncio
async def test_externalize_moves_a_legacy_run_to_the_admission_shape_and_it_resumes(
    client, admin_headers, f, monkeypatch, objects
):
    _admissible(monkeypatch)
    board, job_id, _url = _board_and_job(client, admin_headers, f)
    task_id = client.post(
        f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers
    ).json()["task_id"]
    jobs = _legacy(task_id)
    db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (task_id,))
    rest = db.query_one("SELECT (payload - 'jobs')::text AS t FROM tasks WHERE id = %s", (task_id,))

    [result] = _all("externalize", objects)

    assert result["counts"] == {"externalized": 1}
    payload = _payload(task_id)
    assert "jobs" not in payload and payload["candidate_count"] == len(jobs)
    assert runs.run_jobs(payload, objects) == jobs
    # Everything else in the payload is the same jsonb, byte for byte.
    assert (
        db.query_one(
            "SELECT (payload - 'jobs_ref' - 'candidate_count')::text AS t FROM tasks WHERE id = %s",
            (task_id,),
        )
        == rest
    )

    seen = []

    async def fake_batch(tid, cfg, snapshot, batch_jobs, hooks, **kwargs):
        seen.append([job["id"] for job in batch_jobs])
        hooks.complete()

    monkeypatch.setattr(managed_task, "execute_batch", fake_batch)
    await managed_task.handle_run_managed_board_batch(task_id, _payload(task_id))
    await managed_task.handle_run_managed_board_batch(task_id, _payload(task_id))
    assert seen == [[job_id], [job_id]]


def test_externalize_converts_every_inline_run_in_bounded_resumable_steps(f, objects):
    # Every status: an in-flight run reads its candidates only through
    # run_jobs, so converting it under the handler is safe.
    inline = [
        _run(f, status=status, jobs=[{"id": n, "url": f"u{n}"}])
        for n, status in enumerate(
            ("done", "cancelled", "failed", "pending", "running", "waiting", "awaiting_batch")
        )
    ]
    inline.append(_run(f, kind="run_managed_board", jobs=JOBS))
    referenced = _run(f, jobs_ref=asdict(objects.put_verified(JOBS)), candidate_count=2)
    other = _run(f, kind="run_filter_chunk", jobs=JOBS)
    before = _texts()

    counted = _all("count", objects)
    assert _count(counted, "inline") == len(inline) and _count(counted, "referenced") == 1
    assert _texts() == before

    results = _all("externalize", objects, limit=3, workers=2)
    assert len(results) > 1 and all(sum(r["counts"].values()) <= 3 for r in results)
    assert _count(results, "externalized") == len(inline)
    assert _count(results, "referenced") == 1
    after = _texts()
    assert after[referenced] == before[referenced] and after[other] == before[other]
    for task_id in inline:
        payload = _payload(task_id)
        original = json.loads(before[task_id])
        assert runs.run_jobs(payload, objects) == original["jobs"]
        assert {k: v for k, v in payload.items() if k not in ("jobs_ref", "candidate_count")} == {
            k: v for k, v in original.items() if k != "jobs"
        }

    again = _all("externalize", objects)
    assert _count(again, "externalized") == 0 and _texts() == after
    verified = _all("verify", objects)
    assert _count(verified, "verified") == len(inline) + 1
    assert all(not r["failed"] for r in verified)


def test_a_concurrent_change_between_upload_and_lock_writes_nothing(f, objects):
    task_id = _run(f, jobs=JOBS)
    changed = [{"id": 3, "url": "u3"}]
    objects.client.after_put = lambda: db.execute(
        "UPDATE tasks SET payload = jsonb_set(payload, '{jobs}', %s) WHERE id = %s",
        (db.jsonb(changed), task_id),
    )

    result = task_jobs.migrate(
        task_jobs.MANAGED_BOARD_RUNS,
        "externalize",
        after=0,
        through=task_id,
        limit=10,
        store=objects,
    )

    assert result["counts"] == {"changed": 1} and result["failed"] == [task_id]
    assert result["after"] == 0 and not result["exhausted"]
    assert _payload(task_id) == {"managed_board_id": 1, "prompt": "p", "jobs": changed}


def test_an_upload_failure_stops_before_the_run_and_writes_nothing(f, objects):
    task_id = _run(f, jobs=JOBS)
    objects.client.fail_put = True
    before = _texts()

    result = task_jobs.migrate(
        task_jobs.MANAGED_BOARD_RUNS,
        "externalize",
        after=0,
        through=task_id,
        limit=10,
        store=objects,
    )

    assert result["counts"] == {"unavailable": 1} and result["after"] == 0
    assert _texts() == before


def test_verify_reports_a_wrong_count_and_a_missing_object(f, objects):
    good = _run(f, jobs=JOBS)
    miscounted = _run(f, jobs=[{"id": 3, "url": "u3"}])
    lost = _run(f, jobs=[{"id": 4, "url": "u4"}])
    _all("externalize", objects)
    db.execute(
        "UPDATE tasks SET payload = payload || '{\"candidate_count\": 5}' WHERE id = %s",
        (miscounted,),
    )
    objects.client.objects.pop((objects.bucket, _payload(lost)["jobs_ref"]["key"]))
    before = _texts()

    [result] = _all("verify", objects)

    assert result["counts"] == {"verified": 1, "unavailable": 2}
    assert result["failed"] == [miscounted, lost] and good not in result["failed"]
    assert _texts() == before


def test_restore_puts_back_exactly_the_original_inline_payload(f, objects):
    tasks = [_run(f, jobs=JOBS), _run(f, status="running", jobs=[{"id": 3, "url": "u3"}])]
    original = _texts()

    _all("externalize", objects)
    assert all("jobs" not in _payload(task_id) for task_id in tasks)
    restored = _all("restore", objects)

    assert _count(restored, "restored") == len(tasks)
    assert _texts() == original
    again = _all("restore", objects)
    assert _count(again, "restored") == 0 and _count(again, "inline") == len(tasks)


def test_a_run_with_neither_shape_is_reported_unavailable_not_empty():
    with pytest.raises(PayloadUnavailable):
        runs.run_jobs({"candidate_count": 2})


def test_reference_with_wrong_count_is_refused(objects):
    ref = asdict(objects.put_verified(JOBS))
    with pytest.raises(PayloadUnavailable):
        runs.run_jobs({"jobs_ref": ref, "candidate_count": 3}, objects)
