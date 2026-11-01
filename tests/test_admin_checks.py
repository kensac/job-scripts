from __future__ import annotations

from api import db
from core.store import Page
from tests.factories import user_id_for_subject


def test_manual_check_addresses_filters_by_hash(client, admin_headers):
    uid = user_id_for_subject("test-admin")
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
    for check in ("nonsense", "filter:abc"):
        bad = client.post(
            "/v1/admin/checks/run", json={"job_id": jid, "check": check}, headers=admin_headers
        )
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INVALID_CHECK", check


def test_manual_check_addresses_a_managed_board_by_hash(client, admin_headers):
    uid = user_id_for_subject("test-admin")
    db.execute(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model) VALUES ('mb-hash', 'MB', %s, 'new grad roles', 'board-hash', 'gpt-6-luna')",
        (uid,),
    )
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://mb.test/1','s','C','T')"
    )
    jid = db.query_one("SELECT id FROM jobs WHERE url = 'https://mb.test/1'")["id"]
    resp = client.post(
        "/v1/admin/checks/run",
        json={"job_id": jid, "check": "hash:board-hash"},
        headers=admin_headers,
    )
    # Past the addressing (no "unknown filter"), stopped at the content guard.
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "NO_CONTENT"


def test_recheck_refetches_and_reports_gone_without_asking_the_model(
    client, admin_headers, monkeypatch
):
    """The reported bug: a recheck over cached text said 'open' for a posting
    that had since started redirecting to a careers page."""
    from api.ai import verdicts
    from core import page_fetches

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES "
        "('https://gone.test/jobs/1','s','HP IQ','SWE')"
    )
    jid = db.query_one("SELECT id FROM jobs WHERE url = 'https://gone.test/jobs/1'")["id"]
    # Stale cache from when the posting was still live.
    page_fetches.record(
        "https://gone.test/jobs/1", "passed", "scraped", "A full and healthy job description. " * 30
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

    monkeypatch.setattr(verdicts, "refresh_page", fake_refresh)
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


def test_a_recheck_runs_on_the_chosen_model_and_that_choice_becomes_the_default(
    client, admin_headers, monkeypatch
):
    """Kanishk: a re-check should offer another model and default to the one
    last used for that option. The choice is per check option, persisted on
    the user's prefs, and offered back with the models the caller may run."""
    from api import ai
    from api.ai import verdicts
    from core.checks import JobClosedResponse

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://rc.test/1','s','C','T')"
    )
    jid = db.query_one("SELECT id FROM jobs WHERE url = 'https://rc.test/1'")["id"]
    db.execute(
        "INSERT INTO group_budgets (group_name, weekly_token_budget, allowed_models) "
        "VALUES ('infra-admins', NULL, ARRAY['gpt-5-nano', 'gpt-5.6-luna']) "
        "ON CONFLICT (group_name) DO UPDATE SET allowed_models = EXCLUDED.allowed_models"
    )
    monkeypatch.setattr("core.routing.server_key", lambda provider: "sk-test")

    async def fake_refresh(url, **kw):
        return Page(1, "A posting body long enough to check."), None

    seen: list[str] = []

    async def fake_parse(cfg, instructions, input_text, response_model):
        seen.append(cfg.model)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if "requires_clearance_or_restrictions" in response_model.model_fields:
            return response_model(
                requires_clearance_or_restrictions=False, reason="", restriction_type=None
            ), usage
        return JobClosedResponse(is_closed=False, reason="still open"), usage

    monkeypatch.setattr(verdicts, "refresh_page", fake_refresh)
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
