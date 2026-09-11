"""A user-triggered run is refused, not queued, while the same run is in
flight, and the view that owns the button says so from server state."""

from __future__ import annotations

import pytest

from api import db


@pytest.fixture(autouse=True)
def _available_owner_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")


def _filter(client, headers, name="strict"):
    r = client.post(
        "/v1/user/filters",
        json={"name": name, "prompt": "only backend roles", "on_ambiguous": "keep"},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_a_filter_run_is_refused_while_one_is_in_flight(client, user_headers, runs_permitted):
    fid = _filter(client, user_headers)
    # A save queues nothing unless the person's group re-judges on change
    # (filter_rejudge_on_change_groups is seeded closed); the person presses
    # Run, and the list shows that run.
    first = client.post(f"/v1/user/filters/{fid}/run", headers=user_headers).json()["task_id"]
    rows = client.get("/v1/user/filters", headers=user_headers).json()
    (row,) = [x for x in rows["filters"] if x["id"] == fid]
    assert row["task"] is not None and row["task"]["status"] == "pending"
    assert row["task"]["id"] == first

    r = client.post(f"/v1/user/filters/{fid}/run", headers=user_headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "IN_PROGRESS" and r.json()["detail"]["task_id"] == first

    # A parent split into chunks waits; still in flight.
    db.execute("UPDATE tasks SET status = 'waiting' WHERE id = %s", (first,))
    assert client.post(f"/v1/user/filters/{fid}/run", headers=user_headers).status_code == 409

    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (first,))
    rows = client.get("/v1/user/filters", headers=user_headers).json()
    assert [x["task"] for x in rows["filters"] if x["id"] == fid] == [None]
    r = client.post(f"/v1/user/filters/{fid}/run", headers=user_headers)
    assert r.status_code == 200 and r.json()["task_id"] != first


def test_run_all_is_one_at_a_time_and_covers_every_filter(client, user_headers, runs_permitted):
    fid = _filter(client, user_headers, "a")
    db.execute("UPDATE tasks SET status = 'done' WHERE kind = 'run_filter'")
    r = client.post("/v1/user/filters/run-all", headers=user_headers)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    assert (
        client.get("/v1/user/filters", headers=user_headers).json()["run_all_task"]["id"] == task_id
    )
    assert client.post("/v1/user/filters/run-all", headers=user_headers).status_code == 409
    # A single filter's run is refused too: run-all is about to judge it.
    assert client.post(f"/v1/user/filters/{fid}/run", headers=user_headers).status_code == 409
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task_id,))
    assert client.get("/v1/user/filters", headers=user_headers).json()["run_all_task"] is None
    assert client.post("/v1/user/filters/run-all", headers=user_headers).status_code == 200


def test_another_persons_run_does_not_block_mine(
    client, user_headers, other_user_headers, runs_permitted
):
    _filter(client, other_user_headers)
    fid = _filter(client, user_headers)
    db.execute(
        "UPDATE tasks SET status = 'done' WHERE kind = 'run_filter' "
        "AND (payload->>'filter_id')::bigint = %s",
        (fid,),
    )
    assert client.post(f"/v1/user/filters/{fid}/run", headers=user_headers).status_code == 200


def test_an_admin_pull_already_queued_is_reported_not_queued_again(client, admin_headers, f):
    """A pull of a board whose task is still queued would wait behind it and
    pull the same listings; the request says which boards were skipped, and
    is refused whole only when every board named is in flight."""
    f.make_source("a")
    f.make_source("b")
    first = client.post("/v1/admin/ingest", json={"sources": ["a"]}, headers=admin_headers)
    assert first.status_code == 200, first.text
    a_task = first.json()["tasks"][0]["task_id"]
    ledger = {
        s["name"]: s
        for s in client.get("/v1/admin/sources", headers=admin_headers).json()["sources"]
    }
    assert ledger["a"]["task"]["id"] == a_task and ledger["b"]["task"] is None

    r = client.post("/v1/admin/ingest", json={"sources": ["a", "b"]}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert [t["source"] for t in r.json()["tasks"]] == ["b"]
    assert r.json()["in_flight"] == [{"source": "a", "task_id": a_task}]

    r = client.post("/v1/admin/ingest", json={"sources": ["a"]}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "IN_PROGRESS"
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (a_task,))
    assert (
        client.post("/v1/admin/ingest", json={"sources": ["a"]}, headers=admin_headers).status_code
        == 200
    )


def test_a_reparse_already_queued_is_refused(client, admin_headers, f):
    job_id = f.make_job()
    r = client.post(f"/v1/admin/jobs/{job_id}/reparse", headers=admin_headers)
    assert r.status_code == 200, r.text
    again = client.post(f"/v1/admin/jobs/{job_id}/reparse", headers=admin_headers)
    assert again.status_code == 409 and again.json()["detail"]["task_id"] == r.json()["task_id"]


def test_repeated_uploads_share_extraction_and_admin_admission(
    client, user_headers, admin_headers, monkeypatch
):
    from api import ssrf

    monkeypatch.setattr(ssrf, "validate_public_url", lambda url: None)
    url = "https://example.com/jobs/shared-extraction"
    response = client.post("/v1/uploads", json={"urls": [url, url]}, headers=user_headers)
    assert response.status_code == 200, response.text
    job_id = response.json()["accepted"][0]["job_id"]
    assert (
        db.query_one("SELECT person_touched_at FROM user_jobs WHERE job_id = %s", (job_id,))[
            "person_touched_at"
        ]
        is not None
    )
    rows = db.query("SELECT id FROM tasks WHERE kind = 'extract_upload'")
    assert len(rows) == 1
    refused = client.post(f"/v1/admin/jobs/{job_id}/reparse", headers=admin_headers)
    assert refused.status_code == 409
    assert refused.json()["detail"]["task_id"] == rows[0]["id"]


def test_repeated_sources_are_one_manual_admission(client, admin_headers, f):
    f.make_source("single")
    response = client.post(
        "/v1/admin/ingest", json={"sources": ["single", "single"]}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["tasks"]) == 1
    assert len(db.query("SELECT id FROM tasks WHERE kind = 'ingest_source'")) == 1


def test_concurrent_reparses_check_conflicts_under_the_job_lock(client, admin_headers, f):
    import time
    from concurrent.futures import ThreadPoolExecutor

    job_id = f.make_job()
    with ThreadPoolExecutor(max_workers=2) as workers:
        with db.transaction():
            db.query_one("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
            responses = [
                workers.submit(
                    client.post, f"/v1/admin/jobs/{job_id}/reparse", headers=admin_headers
                )
                for _ in range(2)
            ]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                db.execute("SELECT pg_stat_clear_snapshot()")
                blocked = db.query_one(
                    "SELECT count(*) AS n FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND query LIKE '%%jobs%%'"
                )
                if blocked["n"] == 2:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError(
                    "both requests must reach the locked job before it is released"
                )
        results = [response.result(timeout=10) for response in responses]
    assert sorted(result.status_code for result in results) == [200, 409]
    assert len(db.query("SELECT id FROM tasks WHERE kind = 'extract_upload'")) == 1
