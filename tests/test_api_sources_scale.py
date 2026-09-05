"""What the surfaces need once there are hundreds of boards: format, employer
and bundle per row on both panes, a delta subscription write, and the admin
lists cut and counted rather than rendered whole."""

from __future__ import annotations

from api import db


def _sources(f):
    for name, url in (
        ("gh_stripe", "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"),
        ("ashby_ramp", "https://api.ashbyhq.com/posting-api/job-board/ramp"),
        ("fulltime", "https://raw.githubusercontent.com/x/y/dev/.github/scripts/listings.json"),
    ):
        f.make_source(name)
        db.execute("UPDATE sources SET listings_url = %s WHERE name = %s", (url, name))
    db.execute("UPDATE sources SET company = 'Stripe' WHERE name = 'gh_stripe'")
    db.execute("UPDATE sources SET company = 'Ramp' WHERE name = 'ashby_ramp'")
    db.execute(
        "INSERT INTO source_groups (name, members) VALUES "
        "('top_tech', ARRAY['gh_stripe', 'ashby_ramp']), ('aggregators', ARRAY['fulltime'])"
    )


def test_a_person_sees_format_employer_and_bundle_on_every_board(client, user_headers, f):
    _sources(f)
    rows = {s["name"]: s for s in client.get("/v1/sources", headers=user_headers).json()["sources"]}
    assert (rows["gh_stripe"]["kind"], rows["gh_stripe"]["company"]) == ("greenhouse", "Stripe")
    assert rows["gh_stripe"]["groups"] == ["top_tech"]
    assert (rows["fulltime"]["kind"], rows["fulltime"]["company"]) == ("sheet_era", None)
    assert rows["fulltime"]["groups"] == ["aggregators"]
    assert rows["ashby_ramp"]["kind"] == "ashby"


def test_a_subscription_changes_by_delta_not_by_replacing_the_whole_set(client, user_headers, f):
    """One toggle used to PUT every enabled name; with 389 boards two toggles
    in flight overwrote each other. A delta touches only what it names, and
    a bundle can be left as well as joined."""
    _sources(f)
    r = client.patch(
        "/v1/user/sources", json={"add": ["gh_stripe", "fulltime"]}, headers=user_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["added"] == ["fulltime", "gh_stripe"] and r.json()["removed"] == []
    assert r.json()["enabled"] == ["fulltime", "gh_stripe"]

    # Leave the bundle, join another board, in one write; the untouched
    # subscription survives.
    r = client.patch(
        "/v1/user/sources",
        json={"add": ["ashby_ramp"], "remove": ["gh_stripe"]},
        headers=user_headers,
    )
    assert r.json() == {
        "ok": True,
        "added": ["ashby_ramp"],
        "removed": ["gh_stripe"],
        "enabled": ["ashby_ramp", "fulltime"],
    }
    # Already in that state: nothing added, nothing removed, nothing lost.
    r = client.patch("/v1/user/sources", json={"add": ["ashby_ramp"]}, headers=user_headers)
    assert r.json()["added"] == [] and r.json()["enabled"] == ["ashby_ramp", "fulltime"]
    # An unknown name refuses the whole write rather than applying half.
    r = client.patch(
        "/v1/user/sources", json={"add": ["nope"], "remove": ["fulltime"]}, headers=user_headers
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "UNKNOWN_SOURCE"
    enabled = client.get("/v1/sources", headers=user_headers).json()["sources"]
    assert sorted(s["name"] for s in enabled if s["enabled"]) == ["ashby_ramp", "fulltime"]


def test_admin_rows_carry_bundle_membership(client, admin_headers, f):
    _sources(f)
    rows = {
        s["name"]: s["groups"]
        for s in client.get("/v1/admin/sources", headers=admin_headers).json()["sources"]
    }
    assert rows == {
        "gh_stripe": ["top_tech"],
        "ashby_ramp": ["top_tech"],
        "fulltime": ["aggregators"],
    }


def test_the_queue_cuts_to_one_board_and_the_requests_badge_counts_everything(
    client, admin_headers, user_headers, f
):
    _sources(f)
    for name in ("gh_stripe", "gh_stripe", "fulltime"):
        f.make_task("ingest_source", {"source": name}, status="done")
    f.make_task("verify_new", {}, status="done")
    rows = client.get(
        "/v1/admin/tasks", params={"source": "gh_stripe"}, headers=admin_headers
    ).json()["rows"]
    assert len(rows) == 2 and all(r["payload"]["source"] == "gh_stripe" for r in rows)

    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (user_headers["X-User-Sub"],))["id"]
    for i in range(3):
        db.execute(
            "INSERT INTO source_requests (user_id, url) VALUES (%s, %s)",
            (uid, f"https://board{i}.example.com"),
        )
    page = client.get(
        "/v1/admin/source-requests", params={"status": "open", "limit": 2}, headers=admin_headers
    ).json()
    assert len(page["rows"]) == 2 and page["has_more"] is True and page["total"] == 3


def test_a_batch_drill_down_is_paged(client, admin_headers):
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, model, requests) "
        "VALUES ('batch_x', 'verify', 'gpt-5-nano', 5)"
    )
    for i in range(5):
        add_ai_result(f"https://b.test/{i}", "passed", "r", "closed", batch_id="batch_x")
    first = client.get(
        "/v1/admin/batches/batch_x/jobs", params={"limit": 2}, headers=admin_headers
    ).json()
    assert len(first["rows"]) == 2 and first["total"] == 5 and first["has_more"] is True
    last = client.get(
        "/v1/admin/batches/batch_x/jobs",
        params={"limit": 2, "offset": 4},
        headers=admin_headers,
    ).json()
    assert len(last["rows"]) == 1 and last["has_more"] is False
    assert {r["url"] for r in first["rows"]}.isdisjoint({r["url"] for r in last["rows"]})


def test_a_board_switched_off_under_a_subscriber_still_saves(client, user_headers, f):
    """Eight feeds went inactive on 2026-09-05 while subscribed; the page's
    next save named them and was refused whole. Held stays held, leaving is
    always allowed, and only a NEW subscription needs the board on."""
    _sources(f)
    client.patch("/v1/user/sources", json={"add": ["gh_stripe", "fulltime"]}, headers=user_headers)
    db.execute("UPDATE sources SET active = false WHERE name = 'fulltime'")
    # The whole-set write the page falls back to, naming the off board it holds.
    r = client.put(
        "/v1/user/sources", json={"enabled": ["gh_stripe", "fulltime"]}, headers=user_headers
    )
    assert r.status_code == 200, r.text
    # A delta that keeps holding it, and one that leaves it.
    r = client.patch("/v1/user/sources", json={"add": ["ashby_ramp"]}, headers=user_headers)
    assert r.status_code == 200 and r.json()["enabled"] == ["ashby_ramp", "fulltime", "gh_stripe"]
    r = client.patch("/v1/user/sources", json={"remove": ["fulltime"]}, headers=user_headers)
    assert r.status_code == 200 and r.json()["removed"] == ["fulltime"]
    # Once let go, an off board cannot be picked up again until it is on.
    r = client.patch("/v1/user/sources", json={"add": ["fulltime"]}, headers=user_headers)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "SOURCE_INACTIVE"
    r = client.put("/v1/user/sources", json={"enabled": ["fulltime"]}, headers=user_headers)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "SOURCE_INACTIVE"
