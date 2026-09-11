from __future__ import annotations

from api import db
from core.filters import build_custom_instructions, compute_prompt_hash


def test_create_filter_returns_task_id_or_run_blocked_and_stores_prompt_hash(client, user_headers):
    resp = client.post(
        "/v1/user/filters",
        json={"name": "visa-sponsor", "prompt": "must sponsor visas"},
        headers=user_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert ("task_id" in body) and ("run_blocked" in body)
    assert body["task_id"] is not None or body["run_blocked"] is not None

    expected_hash = compute_prompt_hash(build_custom_instructions("must sponsor visas", "keep"))
    assert body["prompt_hash"] == expected_hash

    row = db.query_one("SELECT prompt_hash FROM user_filters WHERE id = %s", (body["id"],))
    assert row["prompt_hash"] == expected_hash


def test_duplicate_filter_name_for_same_user_is_conflict(client, user_headers):
    first = client.post(
        "/v1/user/filters", json={"name": "dup-name", "prompt": "prompt one"}, headers=user_headers
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/user/filters", json={"name": "dup-name", "prompt": "prompt two"}, headers=user_headers
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "DUPLICATE_NAME"


def test_presets_list_returns_seeded_presets(client, admin_headers, user_headers):
    created = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Remote Only", "prompt": "must be fully remote"},
        headers=admin_headers,
    )
    assert created.status_code == 200
    preset = created.json()

    listed = client.get("/v1/filter-presets", headers=user_headers)
    assert listed.status_code == 200
    names = {p["name"] for p in listed.json()["presets"]}
    assert preset["name"] in names


def test_adopt_preset_creates_user_filter(client, admin_headers, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (user_headers["X-User-Sub"],))["id"]
    created = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Junior Friendly", "prompt": "no more than 2 years experience required"},
        headers=admin_headers,
    )
    preset_id = created.json()["id"]

    adopted = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert adopted.status_code == 200, adopted.text
    body = adopted.json()
    assert body["name"] == "Junior Friendly"
    assert body["enabled"] is True

    row = db.query_one(
        "SELECT id FROM user_filters WHERE user_id = %s AND name = %s", (uid, "Junior Friendly")
    )
    assert row is not None

    # adopting the same preset twice is rejected
    again = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ALREADY_ADOPTED"

    # Provenance is on the row, so a rename does not turn it back into "Add":
    # the list still says which preset it came from and a re-adopt is still
    # refused. A hand-written filter carries no preset.
    assert body["preset_id"] == preset_id
    renamed = client.patch(
        f"/v1/user/filters/{body['id']}", json={"name": "My junior filter"}, headers=user_headers
    )
    assert renamed.status_code == 200, renamed.text
    listed = {
        f["id"]: f for f in client.get("/v1/user/filters", headers=user_headers).json()["filters"]
    }
    assert listed[body["id"]]["preset_id"] == preset_id
    assert listed[body["id"]]["name"] == "My junior filter"
    assert (
        client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers).status_code
        == 409
    )
    # One filter runs at a time and the adopted one is on, so a hand-written
    # second one comes in disabled.
    own = client.post(
        "/v1/user/filters",
        json={"name": "hand written", "prompt": "no agencies", "enabled": False},
        headers=user_headers,
    )
    assert own.status_code == 200 and own.json()["preset_id"] is None


def _preset(name: str, prompt: str) -> int:
    from api import db

    row = db.query_one(
        "INSERT INTO filter_presets (name, description, prompt, on_ambiguous, active) "
        "VALUES (%s, 'd', %s, 'keep', TRUE) RETURNING id",
        (name, prompt),
    )
    assert row is not None
    return row["id"]


def test_preset_coverage_says_what_it_would_show_before_spending_anything(client, user_headers, f):
    """A preset's cached verdicts are reusable across users because
    prompt_hash is computed from the prompt and on_ambiguous alone. Nothing
    user-specific. That is what lets someone with no API key see a real board
    on day one instead of 7,397 undifferentiated postings."""
    from core.filters import build_custom_instructions, compute_prompt_hash

    preset_id = _preset("cached", "backend roles only")
    phash = compute_prompt_hash(build_custom_instructions("backend roles only", "keep"))
    _, shown = f.make_ready_job(source="s")
    _, hidden = f.make_ready_job(source="s")
    f.make_ready_job(source="s")  # eligible, never judged by this preset
    f.make_verdict(shown, "custom", "passed", prompt_hash=phash)
    f.make_verdict(hidden, "custom", "rejected", prompt_hash=phash)

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert body["eligible_postings"] == 3
    assert cov["would_show_now"] == 1
    assert cov["already_judged"] == 2
    assert cov["needs_ai"] == 1


def test_an_uncovered_preset_reports_zero_rather_than_looking_ready(client, user_headers, f):
    """Adopting a preset with no cached verdicts shows an EMPTY board until AI
    runs, which is worse than the wall it replaces. The numbers ship with the
    preset so the choice is not made blind."""
    preset_id = _preset("uncached", "aerospace only")
    f.make_ready_job(source="s")

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert cov["would_show_now"] == 0
    assert cov["already_judged"] == 0
    assert cov["needs_ai"] == body["eligible_postings"] == 1


def test_coverage_counts_only_postings_that_clear_both_gates(client, user_headers, f):
    """A posting the closed check rejected is not eligible for anyone, so it
    must not inflate what a preset promises."""
    from core.filters import build_custom_instructions, compute_prompt_hash

    preset_id = _preset("gated", "anything")
    phash = compute_prompt_hash(build_custom_instructions("anything", "keep"))
    _, live = f.make_ready_job(source="s", closed="passed", clearance="passed")
    _, dead = f.make_ready_job(source="s", closed="rejected", clearance="passed")
    for url in (live, dead):
        f.make_verdict(url, "custom", "passed", prompt_hash=phash)

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert body["eligible_postings"] == 1
    assert cov["would_show_now"] == 1


def test_coverage_reads_the_latest_custom_verdict(client, user_headers, f):
    from core.filters import build_custom_instructions, compute_prompt_hash

    preset_id = _preset("revised", "changed mind")
    phash = compute_prompt_hash(build_custom_instructions("changed mind", "keep"))
    _, url = f.make_ready_job(source="s")
    f.make_verdict(url, "custom", "passed", prompt_hash=phash)
    f.make_verdict(url, "custom", "rejected", prompt_hash=phash)

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert cov["already_judged"] == 1
    assert cov["would_show_now"] == 0


USER_INSIGHT = "/v1/user/filter-insights/rejection-reasons"


def _reject(f, url: str, phash: str, reason: str) -> None:
    f.make_verdict(url, "custom", "rejected", prompt_hash=phash, reason=reason)


def test_a_user_sees_the_rejections_of_their_own_filter(client, user_headers, f):
    """The person who can fix a misfiring filter is its owner, and the only
    view of what it rejected was admin-only."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="mine", prompt="pay over 200k")
    for _ in range(3):
        _, url = f.make_ready_job(source="s")
        _reject(f, url, flt["prompt_hash"], "Salary not disclosed; cannot confirm the bar.")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    row = next(r for r in body["prompt_versions"] if r["prompt_hash"] == flt["prompt_hash"])
    assert [x["name"] for x in row["filters"]] == ["mine"]
    assert row["totals"]["rejected"] == 3
    assert row["totals"]["rejected_with_reason"] == 3
    assert sum(g["decisions"] for g in row["groups"]) >= 3


def test_a_user_never_sees_another_users_filter(client, user_headers, f):
    """Verdicts are shared by prompt_hash, so scoping has to come from the
    caller's own user_filters rows."""
    stranger = f.make_user()
    theirs = f.make_filter(stranger, name="not-yours", prompt="aerospace only")
    _, url = f.make_ready_job(source="s")
    _reject(f, url, theirs["prompt_hash"], "Not aerospace.")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    hashes = {r["prompt_hash"] for r in body["prompt_versions"]}
    assert theirs["prompt_hash"] not in hashes


def test_the_response_never_leaks_the_owner_embedded_in_filter_name(client, user_headers, f):
    """ai_queries.filter_name is `user1:pay_tier_200` - it embeds the owner.
    Two people adopting the same preset share verdicts, so echoing it would
    hand one of them the other's id and filter names."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="shared-preset", prompt="backend only")
    _, url = f.make_ready_job(source="s")
    from core.store import add_ai_result

    add_ai_result(
        url,
        "rejected",
        "Not backend.",
        "custom",
        prompt_hash=flt["prompt_hash"],
        filter_name="user999:someone-elses-name",
        model="gpt-5-nano",
    )

    raw = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).text
    assert "user999" not in raw
    assert "someone-elses-name" not in raw


def test_groups_report_the_denominator_they_are_actually_of(client, user_headers, f):
    """Buckets only cover rejections that carry a reason. The batched paths
    recorded none for a period, so dividing a group by `rejected` understates
    it - `rejected_with_reason` is the honest denominator."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="partly-reasoned", prompt="pay over 200k")
    _, with_reason = f.make_ready_job(source="s")
    _reject(f, with_reason, flt["prompt_hash"], "Salary not disclosed.")
    _, blank = f.make_ready_job(source="s")
    f.make_verdict(blank, "custom", "rejected", prompt_hash=flt["prompt_hash"], reason="")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    row = next(r for r in body["prompt_versions"] if r["prompt_hash"] == flt["prompt_hash"])
    assert row["totals"]["rejected"] == 2
    assert row["totals"]["rejected_with_reason"] == 1


def test_a_filter_that_decided_nothing_says_so_rather_than_vanishing(client, user_headers, f):
    """Absent reads as "no problems"; a zero row reads as "nothing ran"."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, name="never-ran", prompt="quantum only")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    row = next(r for r in body["prompt_versions"] if r["prompt_hash"] == flt["prompt_hash"])
    assert row["totals"]["evaluated"] == 0
    assert row["sufficient"] is False


def test_two_filter_names_sharing_a_prompt_are_one_row(client, user_headers, f):
    """One prompt here is both "default" and "general". A row per filter name
    would double-count the same decisions."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    a = f.make_filter(uid, name="default", prompt="same text")
    b = f.make_filter(uid, name="general", prompt="same text", enabled=False)
    assert a["prompt_hash"] == b["prompt_hash"]
    _, url = f.make_ready_job(source="s")
    _reject(f, url, a["prompt_hash"], "Nope.")

    body = client.get(f"{USER_INSIGHT}?min_decisions=1", headers=user_headers).json()
    rows = [r for r in body["prompt_versions"] if r["prompt_hash"] == a["prompt_hash"]]
    assert len(rows) == 1
    assert sorted(x["name"] for x in rows[0]["filters"]) == ["default", "general"]
    assert rows[0]["totals"]["rejected"] == 1


def test_one_filter_is_enabled_at_a_time(client, user_headers, admin_headers):
    """A person runs one filter (Kanishk, 2026-09-08: a new account's three
    filters judged the whole catalog three times over). A second enabled
    filter is refused on create, on turning it on, and on adopting a
    preset, with the name of the one that is on; a disabled second is fine,
    and turning the first off frees the slot."""
    first = client.post(
        "/v1/user/filters",
        json={"name": "one", "prompt": "remote only"},
        headers=user_headers,
    ).json()
    assert first["enabled"] is True

    second = client.post(
        "/v1/user/filters",
        json={"name": "two", "prompt": "pays well"},
        headers=user_headers,
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "ONE_FILTER"
    assert second.json()["detail"]["enabled_filter_id"] == first["id"]
    assert '"one" is on' in second.json()["detail"]["message"]
    assert "fold this into it" in second.json()["detail"]["message"]

    parked = client.post(
        "/v1/user/filters",
        json={"name": "two", "prompt": "pays well", "enabled": False},
        headers=user_headers,
    ).json()
    assert parked["enabled"] is False
    turn_on = client.patch(
        f"/v1/user/filters/{parked['id']}", json={"enabled": True}, headers=user_headers
    )
    assert turn_on.status_code == 409 and turn_on.json()["detail"]["code"] == "ONE_FILTER"

    # Editing the one that is on, or re-sending enabled on it, is not a second.
    assert (
        client.patch(
            f"/v1/user/filters/{first['id']}",
            json={"enabled": True, "prompt": "remote only, please"},
            headers=user_headers,
        ).status_code
        == 200
    )

    preset_id = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Remote Only", "prompt": "must be fully remote"},
        headers=admin_headers,
    ).json()["id"]
    adopt = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert adopt.status_code == 409 and adopt.json()["detail"]["code"] == "ONE_FILTER"

    assert (
        client.patch(
            f"/v1/user/filters/{first['id']}", json={"enabled": False}, headers=user_headers
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/v1/user/filters/{parked['id']}", json={"enabled": True}, headers=user_headers
        ).status_code
        == 200
    )


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


def test_a_filter_save_waits_for_the_sweep_unless_the_group_rejudges_on_change(
    client, user_headers
):
    """Seeded closed: a save returns DEFERRED with the sentence the page shows
    and queues nothing; the hourly sweep judges the board. With "*" in
    filter_rejudge_on_change_groups the save queues the run as before, or
    names the budget (Kanishk, 2026-09-09)."""
    from api.routers.filters import DEFERRED_MESSAGE, REJUDGE_GROUPS_KEY

    def gate(groups):
        db.execute(
            "INSERT INTO app_config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (REJUDGE_GROUPS_KEY, db.jsonb(groups)),
        )

    try:
        gate([])
        deferred = client.post(
            "/v1/user/filters",
            json={"name": "deferred", "prompt": "robotics only"},
            headers=user_headers,
        ).json()
        assert deferred["task_id"] is None and deferred["run_blocked"] == "DEFERRED"
        assert deferred["run_blocked_message"] == DEFERRED_MESSAGE
        edited = client.patch(
            f"/v1/user/filters/{deferred['id']}",
            json={"prompt": "robotics or embedded only"},
            headers=user_headers,
        ).json()
        assert edited["task_id"] is None and edited["run_blocked"] == "DEFERRED"
        assert not db.query_one(
            "SELECT 1 FROM tasks WHERE kind = 'run_filter' "
            "AND (payload->>'filter_id')::bigint = %s",
            (deferred["id"],),
        )

        gate(["*"])
        at_once = client.patch(
            f"/v1/user/filters/{deferred['id']}",
            json={"prompt": "embedded only"},
            headers=user_headers,
        ).json()
        assert at_once["run_blocked"] != "DEFERRED"
        assert at_once["task_id"] is not None or at_once["run_blocked"] is not None
    finally:
        gate([])


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


def test_the_button_is_disabled_from_the_same_answer_that_refuses_the_request(client, user_headers):
    """Two spellings of one rule is how a disabled button and an allowed
    endpoint come apart, so the list reports the refusal the route enforces."""
    _make_filter(client, user_headers, "disabled-button")

    listed = client.get("/v1/user/filters", headers=user_headers).json()

    assert listed["run_all_admission"]["allowed"] is False
    assert listed["run_all_admission"]["reason"] == "NOT_PERMITTED"
    for row in listed["filters"]:
        assert row["run_admission"]["allowed"] is False
        assert row["run_admission"]["reason"] == "NOT_PERMITTED"


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
    finally:
        db.execute("DELETE FROM app_config WHERE key = 'filter_run_groups'")
