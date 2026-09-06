"""A user-triggered run is refused, not queued, while the same run is in
flight, and the view that owns the button says so from server state."""

from __future__ import annotations

from api import db


def _filter(client, headers, name="strict"):
    r = client.post(
        "/v1/user/filters",
        json={"name": name, "prompt": "only backend roles", "on_ambiguous": "keep"},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_a_filter_run_is_refused_while_one_is_in_flight(client, user_headers):
    fid = _filter(client, user_headers)
    # Creating a filter queues its first run; the list shows it.
    rows = client.get("/v1/user/filters", headers=user_headers).json()
    (row,) = [x for x in rows["filters"] if x["id"] == fid]
    assert row["task"] is not None and row["task"]["status"] == "pending"
    first = row["task"]["id"]

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


def test_run_all_is_one_at_a_time_and_covers_every_filter(client, user_headers):
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


def test_another_persons_run_does_not_block_mine(client, user_headers, other_user_headers):
    _filter(client, other_user_headers)
    fid = _filter(client, user_headers)
    db.execute(
        "UPDATE tasks SET status = 'done' WHERE kind = 'run_filter' "
        "AND (payload->>'filter_id')::bigint = %s",
        (fid,),
    )
    assert client.post(f"/v1/user/filters/{fid}/run", headers=user_headers).status_code == 200
