from __future__ import annotations

from api import db


def test_an_admin_queues_a_run_past_the_cap_for_one_run_only(
    client, user_headers, admin_headers, monkeypatch
):
    """The shared weekly cap is a group setting; a run an admin queues with
    ignore_budget goes past it for that run alone, the spend still recorded,
    and the cap never moves. The person's own endpoints cannot set it."""
    from api import budget, db

    flt = client.post(
        "/v1/user/filters",
        json={"name": "one", "prompt": "remote only", "enabled": False},
        headers=user_headers,
    ).json()
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (user_headers["X-User-Sub"],))["id"]

    forbidden = client.post(
        "/v1/admin/filters/run",
        json={"user_id": uid, "filter_id": flt["id"], "ignore_budget": True},
        headers=user_headers,
    )
    assert forbidden.status_code == 403

    queued = client.post(
        "/v1/admin/filters/run",
        json={"user_id": uid, "filter_id": flt["id"], "ignore_budget": True},
        headers=admin_headers,
    )
    assert queued.status_code == 200, queued.text
    task = db.query_one(
        "SELECT kind, payload FROM tasks WHERE id = %s", (queued.json()["task_id"],)
    )
    assert task["kind"] == "run_filter" and task["payload"]["ignore_budget"] is True

    # A spent cap blocks the ordinary load and not the flagged one.
    spent = budget.Entitlement(
        owner_key=True, weekly_token_budget=100, spent_this_week=1_000, has_byo_key=False, groups=[]
    )
    monkeypatch.setattr(budget, "get_entitlement", lambda authed: spent)
    monkeypatch.setattr(budget, "resolve_ai_config", lambda user_id, ent: ("cfg", ent.key_source))
    assert budget.load_config(uid)[0].key_source is None
    lifted, _ = budget.load_config(uid, ignore_budget=True)
    assert lifted.key_source == "owner" and lifted.weekly_token_budget is None
    assert spent.weekly_token_budget == 100


def _make_filter(client, headers, name="gated"):
    made = client.post(
        "/v1/user/filters", json={"name": name, "prompt": "keep backend roles"}, headers=headers
    )
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


def test_a_non_admin_cannot_start_a_run_by_hand(client, user_headers):
    """A run re-judges every posting in the catalog against a prompt, which is
    the most expensive thing a button in this product can do.

    Three filter edits on one account cost 10.27 dollars in a day on
    2026-09-08. That closed the automatic path, `filter_rejudge_on_change_groups`,
    and left the manual one open; this is the other half.
    """
    filter_id = _make_filter(client, user_headers, "no-run-for-you")

    one = client.post(f"/v1/user/filters/{filter_id}/run", headers=user_headers)
    every = client.post("/v1/user/filters/run-all", headers=user_headers)

    assert one.status_code == 403, one.text
    assert every.status_code == 403, every.text
    assert one.json()["detail"]["code"] == "NOT_PERMITTED"
    assert every.json()["detail"]["code"] == "NOT_PERMITTED"


def test_the_list_says_the_person_may_not_run_without_overwriting_the_admission(
    client, user_headers
):
    """Permission and admission are two questions, and folding one into the
    other loses an answer.

    `run_all_admission` says whether a run may start NOW: it carries the budget
    reason and the id of a run already in flight. A person who may not start
    one still has the hourly sweep running for them and still wants to watch
    it, so overwriting that decision with the permission refusal would throw
    away the task id for exactly the people who cannot start their own.
    """
    _make_filter(client, user_headers, "may-not-run")
    listed = client.get("/v1/user/filters", headers=user_headers).json()

    assert listed["may_run_by_hand"] is False
    assert "admins" in listed["may_run_message"]
    # Whatever the admission reports, it is the admission's own answer about
    # budget and conflicts. Never the permission one, which is what the first
    # version overwrote it with.
    assert listed["run_all_admission"]["reason"] != "NOT_PERMITTED"
    for row in listed["filters"]:
        assert row["run_admission"]["reason"] != "NOT_PERMITTED"


def test_an_admin_may_still_run(client, admin_headers):
    """The gate is on who presses it, not on the route existing."""
    filter_id = _make_filter(client, admin_headers, "admin-may-run")

    started = client.post(f"/v1/user/filters/{filter_id}/run", headers=admin_headers)

    # Anything but the permission refusal: a budget or in-progress block is
    # this route working, and is not what this test is about.
    assert started.status_code != 403, started.text


def test_opening_the_group_lets_a_non_admin_run(client, user_headers):
    """The rule is a config row rather than a code literal, so it can be
    opened without a deploy."""
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('filter_run_groups', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb(["*"]),),
    )
    try:
        filter_id = _make_filter(client, user_headers, "now-permitted")
        started = client.post(f"/v1/user/filters/{filter_id}/run", headers=user_headers)
        assert started.status_code != 403, started.text
        listed = client.get("/v1/user/filters", headers=user_headers).json()
        assert listed["may_run_by_hand"] is True
        assert listed["may_run_message"] is None
    finally:
        db.execute("DELETE FROM app_config WHERE key = 'filter_run_groups'")
