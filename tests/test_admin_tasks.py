from __future__ import annotations

from api import db
from api.worker import enqueue
from tasks import runtime as tasks_runtime


def test_admin_tasks_before_id_cursor(client, admin_headers):
    ids = [enqueue("run_filter", {"i": i}) for i in range(5)]
    assert all(ids)

    page1 = client.get("/v1/admin/tasks", params={"limit": 2}, headers=admin_headers)
    data1 = page1.json()
    got1 = [r["id"] for r in data1["rows"]]
    assert got1 == sorted(got1, reverse=True)
    assert data1["has_more"] is True

    lowest = got1[-1]
    page2 = client.get(
        "/v1/admin/tasks", params={"limit": 2, "before_id": lowest}, headers=admin_headers
    )
    data2 = page2.json()
    got2 = [r["id"] for r in data2["rows"]]
    assert all(i < lowest for i in got2)
    assert not (set(got1) & set(got2))


def test_cancel_tasks_cancels_only_live_states(client, admin_headers):

    t1 = tasks_runtime.enqueue("run_filter", {"user_id": 1})
    t2 = tasks_runtime.enqueue("run_filter", {"user_id": 2})
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (t2,))
    resp = client.post(
        "/v1/admin/tasks/cancel", json={"ids": [t1, t2, 999999]}, headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] == [t1]
    assert set(body["skipped"]) == {t2, 999999}
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (t1,))
    assert row["status"] == "cancelled" and "admin" in row["error"]
    assert db.query_one("SELECT status FROM tasks WHERE id = %s", (t2,))["status"] == "done"


def test_a_selection_cancels_everything_that_matches_not_one_page_of_it(client, admin_headers, f):
    """Select-all on the queue used to cancel the 200 ids the page held and
    say nothing about the rest. A selection by kind, source or status is one
    write over whatever matches, intersected with ids when both are given."""
    from api import db

    ingest = [f.make_task("ingest_source", {"source": "acme"}) for _ in range(3)]
    other_source = f.make_task("ingest_source", {"source": "other"})
    verify = f.make_task("verify_new", {})
    finished = f.make_task("ingest_source", {"source": "acme"}, status="done")

    r = client.post(
        "/v1/admin/tasks/cancel",
        json={"kind": "ingest_source", "source": "acme"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["cancelled"]) == sorted(ingest)
    statuses = {r["id"]: r["status"] for r in db.query("SELECT id, status FROM tasks")}
    assert statuses[other_source] == "pending" and statuses[verify] == "pending"
    assert statuses[finished] == "done"

    # ids and a selection intersect; a finished id is skipped, not cancelled.
    r = client.post(
        "/v1/admin/tasks/cancel",
        json={"ids": [other_source, verify, finished], "kind": "ingest_source"},
        headers=admin_headers,
    )
    assert r.json() == {"cancelled": [other_source], "skipped": [verify, finished]}

    assert client.post("/v1/admin/tasks/cancel", json={}, headers=admin_headers).status_code == 400
    r = client.post("/v1/admin/tasks/cancel", json={"status": "done"}, headers=admin_headers)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "NOT_CANCELLABLE"
