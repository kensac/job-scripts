from __future__ import annotations

from api import db


def test_admin_source_requests_pagination(client, admin_headers, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (user_headers["X-User-Sub"],))["id"]
    for i in range(3):
        db.execute(
            "INSERT INTO source_requests (user_id, url) VALUES (%s, %s)",
            (uid, f"https://board{i}.example.com"),
        )

    page1 = client.get(
        "/v1/admin/source-requests", params={"status": "open", "limit": 2}, headers=admin_headers
    )
    data1 = page1.json()
    assert len(data1["rows"]) == 2
    assert data1["has_more"] is True

    page2 = client.get(
        "/v1/admin/source-requests",
        params={"status": "open", "limit": 2, "offset": 2},
        headers=admin_headers,
    )
    data2 = page2.json()
    assert len(data2["rows"]) == 1
    assert data2["has_more"] is False


def test_delete_source_refuses_while_in_use_then_succeeds(client, admin_headers):
    db.execute("INSERT INTO sources (name, listings_url) VALUES ('doomed', 'https://x')")
    db.execute("INSERT INTO source_groups (name, members) VALUES ('grp', ARRAY['doomed','other'])")
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://d.test/1','doomed','C','T')"
    )

    resp = client.delete("/v1/admin/sources/doomed", headers=admin_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "SOURCE_IN_USE"
    assert db.query_one("SELECT 1 FROM sources WHERE name = 'doomed'") is not None

    db.execute("DELETE FROM jobs WHERE source = 'doomed'")
    resp = client.delete("/v1/admin/sources/doomed", headers=admin_headers)
    assert resp.status_code == 200
    assert db.query_one("SELECT 1 FROM sources WHERE name = 'doomed'") is None
    # Group membership is cleaned so nothing points at a ghost.
    assert db.query_one("SELECT members FROM source_groups WHERE name = 'grp'")["members"] == [
        "other"
    ]
    assert client.delete("/v1/admin/sources/doomed", headers=admin_headers).status_code == 404


def test_delete_source_force_overrides_and_group_delete_works(client, admin_headers):
    db.execute("INSERT INTO sources (name, listings_url) VALUES ('forced', 'https://x')")
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://f.test/1','forced','C','T')"
    )
    resp = client.delete("/v1/admin/sources/forced?force=true", headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["was_attached"]["jobs"] == 1

    db.execute("INSERT INTO source_groups (name, members) VALUES ('empty-grp', ARRAY[]::text[])")
    assert (
        client.delete("/v1/admin/source-groups/empty-grp", headers=admin_headers).status_code == 200
    )
    assert (
        client.delete("/v1/admin/source-groups/empty-grp", headers=admin_headers).status_code == 404
    )


def test_sources_report_when_they_last_produced_a_new_posting(client, admin_headers, f):
    """last_ingest_at says the fetch worked. last_new_posting_at says it found
    anything we had not already seen. They diverge, and the gap is the only
    signal that retires a source: fulltime_ouckah has 215 successful ingests
    and has produced nothing since the reseed, reporting green every hour.
    """
    from api import db

    f.make_source("productive")
    f.make_source("green-but-dead")
    f.make_job(source="productive")
    for name in ("productive", "green-but-dead"):
        task_id = f.make_task("ingest_source", {"source": name}, status="done")
        db.execute("UPDATE tasks SET finished_at = now() WHERE id = %s", (task_id,))

    rows = {
        r["name"]: r
        for r in client.get("/v1/admin/sources", headers=admin_headers).json()["sources"]
    }
    assert rows["productive"]["last_ingest_at"] is not None
    assert rows["productive"]["last_new_posting_at"] is not None
    # Ingested successfully, produced nothing. Previously indistinguishable.
    assert rows["green-but-dead"]["last_ingest_at"] is not None
    assert rows["green-but-dead"]["last_new_posting_at"] is None


def test_a_board_that_never_names_its_company_needs_one_on_the_source(client, admin_headers):
    """Lever, Ashby and Workday list a company's own openings and never say
    whose. Without a company on the row every posting would land with an
    empty company, and the mail matcher has nothing to match against."""
    lever = {
        "name": "palantir",
        "listings_url": "https://api.lever.co/v0/postings/palantir?mode=json",
    }
    r = client.post("/v1/admin/sources", json=lever, headers=admin_headers)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "COMPANY_REQUIRED"

    r = client.post(
        "/v1/admin/sources",
        json={**lever, "company": "Palantir", "title_pattern": r"new grad|intern"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    listed = next(
        s
        for s in client.get("/v1/admin/sources", headers=admin_headers).json()["sources"]
        if s["name"] == "palantir"
    )
    # The list carries the company but not the pattern; the row does.
    assert listed["company"] == "Palantir" and "title_pattern" not in listed
    row = client.get("/v1/admin/sources/palantir", headers=admin_headers).json()
    assert (row["company"], row["title_pattern"], row["kind"]) == (
        "Palantir",
        "new grad|intern",
        "lever",
    )
    assert client.get("/v1/admin/sources/nope", headers=admin_headers).status_code == 404

    # A patch is checked against the merged row: blanking the company on a
    # Lever board is refused, and an aggregator never needed one.
    r = client.patch("/v1/admin/sources/palantir", json={"company": " "}, headers=admin_headers)
    assert r.status_code == 400
    r = client.post(
        "/v1/admin/sources",
        json={
            "name": "speedy",
            "listings_url": "https://raw.githubusercontent.com/x/y/main/README.md",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text


def test_a_title_pattern_that_cannot_compile_is_refused_up_front(client, admin_headers):
    """Ingest compiles the pattern an hour later; a bad one would surface only
    as a failed ingest, so the route refuses it while the admin is looking."""
    body = {
        "name": "spacex",
        "listings_url": "https://boards-api.greenhouse.io/v1/boards/spacex/jobs",
        "title_pattern": "(",
    }
    r = client.post("/v1/admin/sources", json=body, headers=admin_headers)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BAD_TITLE_PATTERN"
    r = client.post("/v1/admin/sources", json={**body, "title_pattern": ""}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.patch("/v1/admin/sources/spacex", json={"title_pattern": "["}, headers=admin_headers)
    assert r.status_code == 400
    r = client.patch(
        "/v1/admin/sources/spacex", json={"title_pattern": "early career"}, headers=admin_headers
    )
    assert r.status_code == 200 and r.json()["title_pattern"] == "early career"


def test_sources_carry_their_format_and_a_switch_flips_a_whole_category(client, admin_headers, f):
    """A category is a way of selecting rows for the one flag that already
    stops scraping and AI spend. The top level is the format read off the URL,
    the level below is a bundle; the response says what it selected and what it
    actually changed, so an already-off board is not reported as switched."""
    from api import db

    for name, url in (
        ("wd_boeing", "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs"),
        (
            "wd_ngc",
            "https://ngc.wd1.myworkdayjobs.com/wday/cxs/ngc/Northrop_Grumman_External_Site/jobs",
        ),
        ("gh_stripe", "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"),
        ("gh_janestreet", "https://boards-api.greenhouse.io/v1/boards/janestreet/jobs"),
    ):
        f.make_source(name)
        db.execute("UPDATE sources SET listings_url = %s WHERE name = %s", (url, name))
    db.execute("UPDATE sources SET active = false WHERE name = 'wd_ngc'")
    db.execute("INSERT INTO source_groups (name, members) VALUES ('quant', ARRAY['gh_janestreet'])")

    kinds = {
        s["name"]: s["kind"]
        for s in client.get("/v1/admin/sources", headers=admin_headers).json()["sources"]
    }
    assert kinds == {
        "wd_boeing": "workday",
        "wd_ngc": "workday",
        "gh_stripe": "greenhouse",
        "gh_janestreet": "greenhouse",
    }

    r = client.post(
        "/v1/admin/sources/switch",
        json={"active": False, "kind": "workday"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    # wd_ngc was already off: selected, not changed.
    assert r.json() == {
        "active": False,
        "ingest_interval_hours": None,
        "selected": ["wd_boeing", "wd_ngc"],
        "changed": ["wd_boeing"],
    }
    active = {
        r["name"]: r["active"] for r in db.query("SELECT name, active FROM sources ORDER BY name")
    }
    assert active == {
        "gh_janestreet": True,
        "gh_stripe": True,
        "wd_boeing": False,
        "wd_ngc": False,
    }

    r = client.post(
        "/v1/admin/sources/switch",
        json={"active": False, "group": "quant", "names": ["wd_ngc"]},
        headers=admin_headers,
    )
    assert r.json()["changed"] == ["gh_janestreet"]
    assert r.json()["selected"] == ["gh_janestreet", "wd_ngc"]

    r = client.post(
        "/v1/admin/sources/switch",
        json={"active": True, "kind": "workday", "group": "quant"},
        headers=admin_headers,
    )
    assert r.json()["changed"] == ["gh_janestreet", "wd_boeing", "wd_ngc"]

    assert (
        client.post(
            "/v1/admin/sources/switch", json={"active": True}, headers=admin_headers
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/admin/sources/switch",
            json={"active": True, "group": "nope"},
            headers=admin_headers,
        ).status_code
        == 404
    )


def test_the_fetch_retry_window_is_persisted_config_with_a_floor(client, admin_headers):
    """The window used to be a constant. It is a row an admin edits; the seed
    is its default, and the route refuses the values that would defeat it
    (zero hours is the hourly hammering the key exists to stop, and a bool is
    an int to isinstance)."""
    from api import db
    from tasks.board import fetch_retry_interval

    assert (
        client.get("/v1/admin/config", headers=admin_headers).json()["config"][
            "fetch_retry_after_hours"
        ]
        == 24
    )
    assert fetch_retry_interval() == "24 hours"

    r = client.put(
        "/v1/admin/config/fetch_retry_after_hours", json={"value": 6}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert db.get_config("fetch_retry_after_hours") == 6
    assert fetch_retry_interval() == "6 hours"

    for bad in (0, -3, True):
        r = client.put(
            "/v1/admin/config/fetch_retry_after_hours", json={"value": bad}, headers=admin_headers
        )
        assert r.status_code in (400, 422), (bad, r.text)
    assert db.get_config("fetch_retry_after_hours") == 6


def test_a_bundle_moves_to_a_daily_pull_in_one_write(client, admin_headers, f):
    """The interval is a column the scheduler reads, set in bulk through the
    same switch as the on/off flag, so a few hundred boards that post a new
    entry-level role a few times a month stop costing 24 pulls a day."""
    from api import db

    for name in ("ashby_a", "ashby_b", "gh_hourly"):
        f.make_source(name)
    db.execute(
        "INSERT INTO source_groups (name, members) VALUES ('startups', ARRAY['ashby_a', 'ashby_b'])"
    )
    db.execute("UPDATE sources SET ingest_interval_hours = 24 WHERE name = 'ashby_b'")

    r = client.post(
        "/v1/admin/sources/switch",
        json={"ingest_interval_hours": 24, "group": "startups"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    # ashby_b was already daily: selected, not changed; active untouched.
    assert r.json() == {
        "active": None,
        "ingest_interval_hours": 24,
        "selected": ["ashby_a", "ashby_b"],
        "changed": ["ashby_a"],
    }
    rows = {
        s["name"]: (s["active"], s["ingest_interval_hours"])
        for s in client.get("/v1/admin/sources", headers=admin_headers).json()["sources"]
    }
    assert rows == {"ashby_a": (True, 24), "ashby_b": (True, 24), "gh_hourly": (True, 1)}

    # Both at once, and the bounds: a week is the ceiling, zero is refused.
    r = client.post(
        "/v1/admin/sources/switch",
        json={"active": False, "ingest_interval_hours": 168, "names": ["gh_hourly"]},
        headers=admin_headers,
    )
    assert r.json()["changed"] == ["gh_hourly"]
    for bad in ({"names": ["gh_hourly"]}, {"names": ["gh_hourly"], "ingest_interval_hours": 0}):
        assert client.post(
            "/v1/admin/sources/switch", json=bad, headers=admin_headers
        ).status_code in (400, 422)
    r = client.post(
        "/v1/admin/sources",
        json={
            "name": "gh_new",
            "listings_url": "https://boards-api.greenhouse.io/v1/boards/new/jobs",
            "ingest_interval_hours": 12,
        },
        headers=admin_headers,
    )
    assert r.status_code == 200 and r.json()["ingest_interval_hours"] == 12
    r = client.patch(
        "/v1/admin/sources/gh_new", json={"ingest_interval_hours": 6}, headers=admin_headers
    )
    assert r.status_code == 200 and r.json()["ingest_interval_hours"] == 6


def test_one_board_has_one_active_reader(client, admin_headers, f):
    """Workday and Ashby read the path without regard to case, so a source
    whose URL differs only in case reads the same board under different
    posting URLs: each posting verified, filtered and shown twice. A second
    active reader is refused one at a time and left off by a bulk switch."""
    url = "https://tjx.wd1.myworkdayjobs.com/wday/cxs/tjx/TJX_EXTERNAL/jobs"
    copy = {"name": "wd_tjx_companies", "listings_url": url.lower(), "company": "TJX"}
    f.make_source("wd_tjx")
    db.execute("UPDATE sources SET listings_url = %s WHERE name = 'wd_tjx'", (url,))

    r = client.post("/v1/admin/sources", json=copy, headers=admin_headers)
    assert r.status_code == 409
    assert (r.json()["detail"]["code"], r.json()["detail"]["other"]) == (
        "DUPLICATE_BOARD",
        "wd_tjx",
    )
    r = client.post("/v1/admin/sources", json={**copy, "active": False}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.patch(
        "/v1/admin/sources/wd_tjx_companies", json={"active": True}, headers=admin_headers
    )
    assert r.status_code == 409
    f.make_source("other_board")
    r = client.patch(
        "/v1/admin/sources/other_board",
        json={"listings_url": url, "company": "TJX"},
        headers=admin_headers,
    )
    assert r.status_code == 409

    # A bundle holding the copy beside its keeper switches on without the copy.
    db.execute("UPDATE sources SET active = false WHERE name = 'wd_tjx'")
    db.execute(
        "INSERT INTO source_groups (name, members) "
        "VALUES ('f500', ARRAY['wd_tjx', 'wd_tjx_companies'])"
    )
    r = client.post(
        "/v1/admin/sources/switch",
        json={"active": True, "group": "f500", "ingest_interval_hours": 24},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["wd_tjx", "wd_tjx_companies"]
    assert db.query(
        "SELECT name, active, ingest_interval_hours FROM sources "
        "WHERE name LIKE 'wd_tjx%%' ORDER BY name"
    ) == [
        {"name": "wd_tjx", "active": True, "ingest_interval_hours": 24},
        {"name": "wd_tjx_companies", "active": False, "ingest_interval_hours": 24},
    ]


def test_an_admin_patch_refuses_null_for_a_column_that_cannot_hold_one(client, admin_headers):
    """An explicit null used to reach the UPDATE and fail as a 500 on a NOT NULL
    column. Omitting the field leaves it; null is refused before any write."""
    r = client.post(
        "/v1/admin/sources",
        json={"name": "nulls", "listings_url": "https://boards-api.greenhouse.io/v1/boards/n/jobs"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    for field in ("listings_url", "description", "active", "ingest_interval_hours"):
        r = client.patch("/v1/admin/sources/nulls", json={field: None}, headers=admin_headers)
        assert r.status_code == 422, (field, r.text)
    r = client.patch("/v1/admin/sources/nulls", json={"company": None}, headers=admin_headers)
    assert r.status_code == 200, r.text

    r = client.post(
        "/v1/admin/filter-presets", json={"name": "p", "prompt": "x"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    preset_id = r.json()["id"]
    for field in ("name", "description", "prompt", "on_ambiguous", "fail_closed", "active"):
        r = client.patch(
            f"/v1/admin/filter-presets/{preset_id}", json={field: None}, headers=admin_headers
        )
        assert r.status_code == 422, (field, r.text)

    job = db.query_one(
        "INSERT INTO jobs (url, source, company, title) "
        "VALUES ('https://nulls.test/1', 'nulls', 'C', 'T') RETURNING id"
    )
    assert job is not None
    for field in ("company", "title", "locations", "terms", "active"):
        r = client.patch(f"/v1/admin/jobs/{job['id']}", json={field: None}, headers=admin_headers)
        assert r.status_code == 422, (field, r.text)
