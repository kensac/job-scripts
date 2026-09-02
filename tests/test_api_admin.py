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
    # than the addressing — which is what proves the hash resolved.
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
