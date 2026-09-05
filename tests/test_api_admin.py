from __future__ import annotations

import os

from api import db
from api.tasks import runtime as tasks_runtime
from api.worker import enqueue
from core.store import add_ai_result

SERVICE_TOKEN = os.environ["JOBTRACKER_SERVICE_TOKEN"]


def _headers(sub: str, email: str, groups: list) -> dict:
    return {
        "X-Service-Token": SERVICE_TOKEN,
        "X-User-Sub": sub,
        "X-User-Email": email,
        "X-User-Name": sub,
        "X-User-Groups": ",".join(groups),
    }


def _bootstrap(client, sub: str, groups=("jobtracker-users-internal",)) -> dict:
    headers = _headers(sub, f"{sub}@example.com", list(groups))
    resp = client.post("/v1/users/bootstrap", headers=headers)
    assert resp.status_code == 200, resp.text
    return headers


def test_admin_route_forbidden_for_regular_user(client, user_headers):
    resp = client.get("/v1/admin/users", headers=user_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


def test_admin_users_pagination(client, admin_headers):
    _bootstrap(client, "user-a")
    _bootstrap(client, "user-b")

    page1 = client.get("/v1/admin/users", params={"limit": 2}, headers=admin_headers)
    data1 = page1.json()
    assert len(data1["users"]) == 2
    assert data1["has_more"] is True

    page2 = client.get("/v1/admin/users", params={"limit": 2, "offset": 2}, headers=admin_headers)
    data2 = page2.json()
    assert len(data2["users"]) == 1
    assert data2["has_more"] is False


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


def test_admin_failures_breakdown_grouped_by_worker_and_host(client, admin_headers):
    add_ai_result("https://hosta.example.com/1", "failed", check_type="closed", error="boom")
    add_ai_result("https://hosta.example.com/2", "failed", check_type="closed", error="boom")
    add_ai_result("https://hostb.example.com/1", "failed", check_type="clearance", error="boom2")

    resp = client.get("/v1/admin/failures", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    by_host = {(r["host"], r["check_type"]): r["failures"] for r in rows}
    assert by_host[("hosta.example.com", "closed")] == 2
    assert by_host[("hostb.example.com", "clearance")] == 1
    assert all(r["worker"] for r in rows)


def test_admin_signups_toggle_blocks_new_but_not_existing(client, admin_headers, user_headers):
    toggled = client.put(
        "/v1/admin/config/signups_enabled", json={"value": False}, headers=admin_headers
    )
    assert toggled.status_code == 200

    new_headers = _headers("brand-new-user", "new@example.com", ["jobtracker-users-public"])
    blocked = client.post("/v1/users/bootstrap", headers=new_headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "SIGNUPS_DISABLED"

    # an already-registered user (created by the user_headers fixture before
    # the toggle) is unaffected.
    still_ok = client.post("/v1/users/bootstrap", headers=user_headers)
    assert still_ok.status_code == 200


def test_admin_group_budgets_roundtrip(client, admin_headers):
    put = client.put(
        "/v1/admin/group-budgets/test-group",
        json={"weekly_token_budget": 12345, "allowed_models": ["gpt-5-nano"]},
        headers=admin_headers,
    )
    assert put.status_code == 200

    listed = client.get("/v1/admin/group-budgets", headers=admin_headers)
    assert listed.status_code == 200
    data = listed.json()
    row = next(g for g in data["groups"] if g["group_name"] == "test-group")
    assert row["weekly_token_budget"] == 12345
    assert row["allowed_models"] == ["gpt-5-nano"]
    assert "gpt-5-nano" in data["catalog_models"]


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


def test_invites_unconfigured_returns_503_and_empty_list(client, admin_headers):
    resp = client.post("/v1/admin/invites", json={"email": "new@user.com"}, headers=admin_headers)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "INVITES_NOT_CONFIGURED"
    resp = client.get("/v1/admin/invites", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == {"rows": [], "configured": False}


def test_invite_rejects_bad_email(client, admin_headers):
    resp = client.post("/v1/admin/invites", json={"email": "not-an-email"}, headers=admin_headers)
    assert resp.status_code == 422


def test_query_options_serves_live_vocabularies(client, admin_headers):
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('src-a', 'https://x') ON CONFLICT DO NOTHING"
    )
    add_ai_result("https://o.test/1", "passed", "r", "closed", config_name="verify-batch")
    resp = client.get("/v1/admin/queries/options", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "src-a" in body["sources"]
    assert "closed" in body["check_types"]
    assert "verify-batch" in body["contexts"]


def test_queries_filter_by_source(client, admin_headers):
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES "
        "('https://s.test/a', 'src-x', 'A', 'T'), ('https://s.test/b', 'src-y', 'B', 'T')"
    )
    add_ai_result("https://s.test/a", "passed", "r", "closed")
    add_ai_result("https://s.test/b", "passed", "r", "closed")
    resp = client.get("/v1/admin/queries?sources=src-x", headers=admin_headers)
    urls = [r["url"] for r in resp.json()["rows"]]
    assert "https://s.test/a" in urls and "https://s.test/b" not in urls


def test_batch_jobs_drilldown_reports_per_job_cost(client, admin_headers):
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, model, requests, status) "
        "VALUES ('batch_x1', 'verify', 'gpt-5-nano', 1, 'completed')"
    )
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://b.test/1','s','Acme','SWE')"
    )
    add_ai_result(
        "https://b.test/1",
        "passed",
        "job open",
        "closed",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        batch_id="batch_x1",
    )
    resp = client.get("/v1/admin/batches/batch_x1/jobs", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch"]["provider_batch_id"] == "batch_x1"
    row = body["rows"][0]
    assert row["url"] == "https://b.test/1" and row["prompt_tokens"] == 1000
    assert row["est_cost_usd"] is not None and row["est_cost_usd"] > 0
    assert client.get("/v1/admin/batches/nope/jobs", headers=admin_headers).status_code == 404


def _content_row(url, reason, when_days_ago):
    import datetime

    from core.store import add_ai_result

    add_ai_result(
        url, "passed", reason, "content", input_content="X" * 400, config_name="content-cache"
    )
    stamp = (datetime.datetime.now() - datetime.timedelta(days=when_days_ago)).isoformat()
    db.execute(
        "UPDATE ai_queries SET created_at = %s WHERE id = "
        "(SELECT MAX(id) FROM ai_queries WHERE url = %s)",
        (stamp, url),
    )


def test_health_detects_ats_collapse_and_resolves_when_it_recovers(client, admin_headers):
    from api import health

    db.execute(
        "INSERT INTO jobs (url, source, company, title) SELECT "
        "'https://h.test/' || g, 'hsrc', 'C', 'T' FROM generate_series(1,60) g"
    )
    # Baseline week: ATS text served reliably.
    for i in range(1, 31):
        _content_row(f"https://h.test/{i}", "ats text", 4)
    # Last 24h: resolver broke, everything fell back to scraping.
    for i in range(31, 61):
        _content_row(f"https://h.test/{i}", "scraped", 0)

    found = health.detect()
    collapse = [f for f in found if f["kind"] == "ats_text_collapse" and f["subject"] == "hsrc"]
    assert collapse, f"expected an ats collapse alert, got {[f['kind'] for f in found]}"
    assert collapse[0]["severity"] == "critical"

    fresh = health.record(found)
    assert any(f["subject"] == "hsrc" for f in fresh)
    # Re-detecting the same condition must not re-notify.
    assert not [f for f in health.record(health.detect()) if f["subject"] == "hsrc"]

    resp = client.get("/v1/admin/health", headers=admin_headers)
    assert any(a["subject"] == "hsrc" for a in resp.json()["open"])

    # Condition clears -> alert auto-resolves, but only after RESOLVE_GRACE of
    # silence: a detector going quiet for one cycle is not proof it is over.
    health.record([f for f in health.detect() if f["subject"] != "hsrc"])
    assert (
        db.query_one("SELECT resolved_at FROM health_alerts WHERE subject = 'hsrc'")["resolved_at"]
        is None
    )
    db.execute(
        "UPDATE health_alerts SET last_seen = now() - interval '1 day' WHERE subject = 'hsrc'"
    )
    health.record([f for f in health.detect() if f["subject"] != "hsrc"])
    row = db.query_one("SELECT resolved_at FROM health_alerts WHERE subject = 'hsrc'")
    assert row["resolved_at"] is not None


def test_health_ignores_low_volume_noise(client, admin_headers):
    from api import health

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES "
        "('https://q.test/1','quiet','C','T'), ('https://q.test/2','quiet','C','T')"
    )
    _content_row("https://q.test/1", "ats text", 4)
    _content_row("https://q.test/2", "scraped", 0)
    assert not [f for f in health.detect() if f["subject"] == "quiet"]


def test_manual_check_addresses_filters_by_hash(client, admin_headers):
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (admin_headers["X-User-Sub"],))["id"]
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) "
        "VALUES (%s, 'byhash', 'keep backend roles', 'hash-xyz')",
        (uid,),
    )
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://mh.test/1','s','C','T')"
    )
    jid = db.query_one("SELECT id FROM jobs WHERE url = 'https://mh.test/1'")["id"]
    resp = client.post(
        "/v1/admin/checks/run",
        json={"job_id": jid, "check": "hash:hash-xyz"},
        headers=admin_headers,
    )
    # No cached content for this job, so it stops at the content guard rather
    # than the addressing, which is what proves the hash resolved.
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "NO_CONTENT"
    bad = client.post(
        "/v1/admin/checks/run", json={"job_id": jid, "check": "nonsense"}, headers=admin_headers
    )
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INVALID_CHECK"


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


def test_recheck_refetches_and_reports_gone_without_asking_the_model(
    client, admin_headers, monkeypatch
):
    """The reported bug: a recheck over cached text said 'open' for a posting
    that had since started redirecting to a careers page."""
    from api import verdicts
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES "
        "('https://gone.test/jobs/1','s','HP IQ','SWE')"
    )
    jid = db.query_one("SELECT id FROM jobs WHERE url = 'https://gone.test/jobs/1'")["id"]
    # Stale cache from when the posting was still live.
    add_ai_result(
        "https://gone.test/jobs/1",
        "passed",
        "content cached",
        "content",
        input_content="A full and healthy job description. " * 30,
    )

    async def fake_refresh(url, company="", job_title="", context="manual"):
        verdicts.record_manual(
            url=url,
            check_type="closed",
            rejected=True,
            reason="posting redirects away (board index or careers page)",
            company=company,
            job_title=job_title,
            context=context,
        )
        return None, "redirected_away"

    monkeypatch.setattr(verdicts, "refresh_content", fake_refresh)
    resp = client.post(
        "/v1/admin/checks/run", json={"job_id": jid, "check": "closed"}, headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected" and "redirects away" in body["reason"]
    assert body["tokens"] == 0  # no model call needed to know it's gone
    assert body["closure_signal"] == "redirected_away"  # explicit, not inferred
    latest = db.query_one(
        "SELECT status FROM ai_queries WHERE url = 'https://gone.test/jobs/1' "
        "AND check_type = 'closed' ORDER BY id DESC LIMIT 1"
    )
    assert latest["status"] == "rejected"


def test_rate_spike_ignores_expiring_rechecks_but_catches_fresh_misclassification(
    client, admin_headers
):
    """The first live alert was a reverify backlog on a fast-expiring board:
    real closures, but coverage changing rather than anything breaking. Only
    freshly-seen jobs being written off means something upstream is wrong."""
    import datetime

    from api import health
    from core.store import add_ai_result

    def closed_row(url, status, days_ago, company):
        add_ai_result(url, status, "", "closed", company=company)
        stamp = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
        db.execute(
            "UPDATE ai_queries SET created_at = %s WHERE id = "
            "(SELECT MAX(id) FROM ai_queries WHERE url = %s)",
            (stamp, url),
        )

    # One company per job: a rate built from a single employer's bulk drop is
    # capped at MAX_PER_COMPANY, which is the point of the cap.
    db.execute(
        "INSERT INTO jobs (url, source, company, title) SELECT "
        "'https://exp.test/' || g, 'expsrc', 'co' || g, 'T' FROM generate_series(1,120) g"
    )
    # Baseline week: fresh jobs arrive open.
    for i in range(1, 61):
        closed_row(f"https://exp.test/{i}", "passed", 4, f"co{i}")
    # Last 24h: those same jobs are RE-checked and have since expired.
    for i in range(1, 61):
        closed_row(f"https://exp.test/{i}", "rejected", 0, f"co{i}")
    assert not [f for f in health.detect() if f["subject"] == "expsrc"], (
        "expiring re-checks must not read as breakage"
    )

    # Now the real signal: brand-new jobs written off on arrival.
    for i in range(61, 121):
        closed_row(f"https://exp.test/{i}", "rejected", 0, f"co{i}")
    spike = [f for f in health.detect() if f["subject"] == "expsrc"]
    assert spike and spike[0]["kind"] == "closed_rate_spike"
    assert "newly-seen" in spike[0]["message"]


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
    from api.tasks.board import fetch_retry_interval

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


def test_a_recheck_runs_on_the_chosen_model_and_that_choice_becomes_the_default(
    client, admin_headers, monkeypatch
):
    """Kanishk: a re-check should offer another model and default to the one
    last used for that option. The choice is per check option, persisted on
    the user's prefs, and offered back with the models the caller may run."""
    from api import ai, verdicts
    from core.pittcsc_simplify import JobClosedResponse

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://rc.test/1','s','C','T')"
    )
    jid = db.query_one("SELECT id FROM jobs WHERE url = 'https://rc.test/1'")["id"]
    db.execute(
        "INSERT INTO group_budgets (group_name, weekly_token_budget, allowed_models) "
        "VALUES ('infra-admins', NULL, ARRAY['gpt-5-nano', 'gpt-5.6-luna']) "
        "ON CONFLICT (group_name) DO UPDATE SET allowed_models = EXCLUDED.allowed_models"
    )
    monkeypatch.setattr(ai, "server_key", lambda provider: "sk-test")

    async def fake_refresh(url, **kw):
        return "A posting body long enough to check.", None

    seen: list[str] = []

    async def fake_parse(cfg, instructions, input_text, response_model):
        seen.append(cfg.model)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if "requires_clearance_or_restrictions" in response_model.model_fields:
            return response_model(
                requires_clearance_or_restrictions=False, reason="", restriction_type=None
            ), usage
        return JobClosedResponse(is_closed=False, reason="still open"), usage

    monkeypatch.setattr(verdicts, "refresh_content", fake_refresh)
    monkeypatch.setattr(ai, "parse", fake_parse)

    opts = client.get("/v1/admin/checks/options", headers=admin_headers).json()
    assert "gpt-5.6-luna" in opts["models"] and opts["defaults"] == {}

    r = client.post(
        "/v1/admin/checks/run",
        json={"job_id": jid, "check": "closed", "model": "gpt-5.6-luna"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "gpt-5.6-luna" and seen == ["gpt-5.6-luna"]
    row = db.query_one(
        "SELECT model FROM ai_queries WHERE url = 'https://rc.test/1' AND check_type = 'closed' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert row["model"] == "gpt-5.6-luna"

    # Next time, no model given: the last choice for THIS option is the default.
    r = client.post(
        "/v1/admin/checks/run", json={"job_id": jid, "check": "closed"}, headers=admin_headers
    )
    assert r.json()["model"] == "gpt-5.6-luna" and seen[-1] == "gpt-5.6-luna"
    assert client.get("/v1/admin/checks/options", headers=admin_headers).json()["defaults"] == {
        "closed": "gpt-5.6-luna"
    }
    # A different option keeps its own default.
    r = client.post(
        "/v1/admin/checks/run",
        json={"job_id": jid, "check": "clearance"},
        headers=admin_headers,
    )
    assert r.json()["model"] == "gpt-5-nano"

    # A model outside what the caller may run is refused with the list.
    r = client.post(
        "/v1/admin/checks/run",
        json={"job_id": jid, "check": "closed", "model": "gpt-5.6-sol"},
        headers=admin_headers,
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "MODEL_NOT_ALLOWED"
