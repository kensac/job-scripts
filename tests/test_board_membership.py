"""The board is a computed membership: a task runs the full predicate, the
read is a lookup, a preference write asks for a recompute, and what the
person uploaded or acted on never waits."""

from __future__ import annotations

import pytest

from api import db, visibility
from api.tasks import board as board_tasks
from tests.test_api_jobs import _insert_job, _job_ids, _pass_closed, _subscribe, _uid


def _board(client, headers, source):
    return _job_ids(client.get("/v1/user/jobs", params={"source": source}, headers=headers).json())


@pytest.mark.asyncio
@pytest.mark.no_board_recompute
async def test_membership_is_computed_by_the_task_and_read_as_a_lookup(client, user_headers):
    uid = _uid(user_headers)
    a = _insert_job("src-bm", "https://x.test/bm1", locations=["Austin, TX"])
    b = _insert_job("src-bm", "https://x.test/bm2", locations=["Singapore"])
    _pass_closed("https://x.test/bm1")
    _pass_closed("https://x.test/bm2")
    _subscribe(uid, "src-bm")
    # Nothing computed yet: the board is empty even though both would pass.
    assert _board(client, user_headers, "src-bm") == set()
    db.execute("DELETE FROM tasks WHERE kind = 'recompute_board'")

    task = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('recompute_board', %s, 'running') RETURNING id",
        (db.jsonb({"user_id": uid}),),
    )
    await board_tasks.handle_recompute_board(task["id"], {"user_id": uid})
    assert _board(client, user_headers, "src-bm") == {a, b}
    body = client.get("/v1/user/jobs", params={"source": "src-bm"}, headers=user_headers).json()
    assert body["board_computed_at"] is not None

    # A preference write asks for a recompute (one task) and, until it runs,
    # the board is what was computed.
    from api.tasks import locations

    locations.store("Singapore", locations.LocationExtract(country="SG", city="Singapore"), "t")
    locations.store("Austin, TX", locations.LocationExtract(country="US", region="TX"), "t")
    locations.store("United States", locations.LocationExtract(country="US"), "t")
    r = client.put(
        "/v1/user/settings",
        json={"criteria": {"included_locations": ["United States"]}},
        headers=user_headers,
    )
    assert r.status_code == 200
    queued = db.query("SELECT id FROM tasks WHERE kind = 'recompute_board' AND status = 'pending'")
    assert len(queued) == 1
    assert _board(client, user_headers, "src-bm") == {a, b}
    await board_tasks.handle_recompute_board(queued[0]["id"], {"user_id": uid})
    assert _board(client, user_headers, "src-bm") == {a}

    # A second write in the same minute is the same task, not a second one.
    client.put("/v1/user/settings", json={"bypass_sponsorship_filter": True}, headers=user_headers)
    assert (
        len(db.query("SELECT id FROM tasks WHERE kind = 'recompute_board' AND status = 'pending'"))
        <= 1
    )


def test_uploaded_and_acted_on_postings_never_wait(client, user_headers):
    uid = _uid(user_headers)
    mine = _insert_job("src-own", "https://x.test/own1", uploaded_by=uid)
    acted = _insert_job("src-own", "https://x.test/own2")
    _subscribe(uid, "src-own")
    client.patch(f"/v1/user/jobs/{acted}", json={"status": "Applied"}, headers=user_headers)
    visible = _job_ids(client.get("/v1/user/jobs", headers=user_headers).json())
    assert mine in visible and acted in visible
    # And the per-object gate agrees with the board.
    assert client.get(f"/v1/user/jobs/{acted}/detail", headers=user_headers).status_code == 200


def test_full_and_fast_agree_after_a_recompute(client, user_headers):
    uid = _uid(user_headers)
    for i in range(3):
        _insert_job("src-eq", f"https://x.test/eq{i}")
        _pass_closed(f"https://x.test/eq{i}")
    _subscribe(uid, "src-eq")
    visibility.recompute(uid)
    full = set(visibility.member_ids(uid))
    fast = {
        r["id"] for r in db.query(visibility.FAST.format(columns="j.id", extra=""), {"uid": uid})
    }
    assert full <= fast and len(full) >= 3
