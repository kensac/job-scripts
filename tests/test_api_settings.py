from __future__ import annotations

# ---------------------------------------------------------------------------
# PUT /v1/user/settings - ai_model / ai_params validation
# ---------------------------------------------------------------------------


def test_invalid_model_not_in_owner_allowlist_is_400(client, admin_headers, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    resp = client.put(
        "/v1/user/settings", json={"ai_model": "not-a-real-model"}, headers=admin_headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_MODEL"


def test_valid_model_from_owner_models_unlimited_group(client, admin_headers, monkeypatch):
    # infra-admins has weekly_token_budget=NULL -> unlimited owner-key policy,
    # so any keyed catalog model is allowed.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    resp = client.put("/v1/user/settings", json={"ai_model": "gpt-5-nano"}, headers=admin_headers)
    assert resp.status_code == 200


def test_budgeted_group_restricted_to_owner_key_models_default_policy(
    client, user_headers, monkeypatch
):
    # jobtracker-users-internal has a finite weekly budget -> default policy is
    # JOBTRACKER_OWNER_KEY_MODELS (gpt-5-nano, gpt-5-mini by default), not the
    # full catalog.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    ok = client.put("/v1/user/settings", json={"ai_model": "gpt-5-nano"}, headers=user_headers)
    assert ok.status_code == 200

    blocked = client.put("/v1/user/settings", json={"ai_model": "gpt-5"}, headers=user_headers)
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "INVALID_MODEL"


def test_invalid_ai_params_for_provider_is_400(client, user_headers):
    # default provider is openai; temperature is only valid for openai_compatible.
    resp = client.put(
        "/v1/user/settings", json={"ai_params": {"temperature": 0.5}}, headers=user_headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_PARAMS"


def test_unknown_keys_are_rejected_but_prefs_stays_open(client, user_headers):
    """Two rules that read as one and are not.

    The MODEL forbids unknown keys, so a client still writing `background`
    after #301 is told rather than answered 200 for a write that stored
    nothing. `prefs` is a jsonb passthrough and its CONTENTS are not a
    schema - the tracker's onboarding keeps its dismissal and its sponsorship
    answer in there, and a forbid that reached inside would delete that
    surface's only durable state.
    """
    rejected = client.put("/v1/user/settings", json={"background": {}}, headers=user_headers)
    assert rejected.status_code == 422

    ok = client.put(
        "/v1/user/settings",
        json={"prefs": {"onboarding:sponsorship-answered": True, "anything": [1, 2]}},
        headers=user_headers,
    )
    assert ok.status_code == 200
    prefs = client.get("/v1/user/settings", headers=user_headers).json()["prefs"]
    assert prefs["onboarding:sponsorship-answered"] is True
    assert prefs["anything"] == [1, 2]


# ---------------------------------------------------------------------------
# PUT /v1/user/settings/api-key
# ---------------------------------------------------------------------------


def test_api_key_openai_compatible_http_base_url_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={
            "api_key": "dummy-key-1",
            "provider": "openai_compatible",
            "base_url": "http://example.com",
        },
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_BASE_URL"


def test_api_key_openai_compatible_ip_literal_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={
            "api_key": "dummy-key-1",
            "provider": "openai_compatible",
            "base_url": "https://8.8.8.8/v1",
        },
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_BASE_URL"


def test_api_key_openai_compatible_missing_base_url_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "dummy-key-1", "provider": "openai_compatible"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "BASE_URL_REQUIRED"


def test_api_key_bogus_provider_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "dummy-key-1", "provider": "bogus-provider"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_PROVIDER"


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


def test_models_no_byo_key_reflects_owner_allowlist(client, admin_headers, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    resp = client.get("/v1/models", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["providers"], "expected at least one provider from the owner allowlist"
    # Every provider is present; only the server-keyed allowlist is eligible,
    # and everything else says why in words a person reads on the page.
    providers = {p["provider"] for p in data["providers"]}
    assert {"openai", "anthropic"} <= providers
    eligible = {p["provider"] for p in data["providers"] if any(m["eligible"] for m in p["models"])}
    assert eligible == {"openai"}
    assert all(m["eligible"] or m["reason"] for p in data["providers"] for m in p["models"])
    assert data["key_source"] == "owner"
    assert "gpt-5-nano" in data["owner_key_models"]


def test_models_after_byo_key_only_that_provider_catalog(client, user_headers):
    put = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "sk-test-byo-key", "provider": "openai"},
        headers=user_headers,
    )
    assert put.status_code == 200

    resp = client.get("/v1/models", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["providers"]) == 1
    assert data["providers"][0]["provider"] == "openai"
    assert data["key_source"] == "byo"


def test_email_digest_toggle_generates_token_and_unsubscribe_works(client, user_headers):
    from api import db

    resp = client.put("/v1/user/settings", json={"email_digest": True}, headers=user_headers)
    assert resp.status_code == 200
    row = db.query_one(
        "SELECT email_digest, digest_token FROM user_settings us JOIN users u ON u.id = us.user_id WHERE u.sub = %s",
        (user_headers["X-User-Sub"],),
    )
    assert row["email_digest"] is True and row["digest_token"]
    service_only = {"X-Service-Token": user_headers["X-Service-Token"]}
    resp = client.get(f"/v1/digest/unsubscribe?token={row['digest_token']}", headers=service_only)
    assert resp.status_code == 200
    row2 = db.query_one(
        "SELECT email_digest FROM user_settings us JOIN users u ON u.id = us.user_id WHERE u.sub = %s",
        (user_headers["X-User-Sub"],),
    )
    assert row2["email_digest"] is False
    assert client.get("/v1/digest/unsubscribe?token=bogus", headers=service_only).status_code == 404


def _apply_event(f, user_id: int, job_id: int, kind: str) -> None:
    """A tracked application plus one classified message matched to it."""
    from api import db

    app = db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance) "
        "VALUES (%s, %s, 'Acme', 'Engineer', 'tracker') RETURNING id",
        (user_id, job_id),
    )
    assert app is not None
    msg = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at) VALUES (%s, %s, 'takeout', 'a@b.com', 's', now()) RETURNING id",
        (user_id, f"fn-{job_id}"),
    )
    assert msg is not None
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s, %s, 'high')",
        (msg["id"], kind),
    )
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'ats_company', 'high')",
        (msg["id"], app["id"]),
    )


def test_funnel_counts_stages_with_their_denominator(client, user_headers, f):
    """ "299 of 714" is the sentence that matters, not "42%"."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    _apply_event(f, uid, f.make_job(source="board-a"), "acknowledgement")
    _apply_event(f, uid, f.make_job(source="board-a"), "rejection")
    db.execute(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance) "
        "VALUES (%s, %s, 'Acme', 'Engineer', 'tracker')",
        (uid, f.make_job(source="board-a")),
    )

    body = client.get("/v1/user/funnel", headers=user_headers).json()
    stages = {s["stage"]: s for s in body["overall"]["stages"]}
    assert body["overall"]["applications"] == 3
    assert stages["acknowledged"]["reached"] == 1
    assert stages["acknowledged"]["of"] == 3
    assert stages["rejected"]["reached"] == 1


def test_funnel_omits_the_offer_stage_and_says_so(client, user_headers, f):
    """71 applications reach `offer` against 53 reaching interview_invite.
    A funnel reading "more offers than interviews" discredits every number
    beside it, so the stage is omitted and the omission is stated."""
    body = client.get("/v1/user/funnel", headers=user_headers).json()
    assert "offer" not in {s["stage"] for s in body["overall"]["stages"]}
    excluded = {e["stage"]: e["reason"] for e in body["excluded_stages"]}
    assert "offer" in excluded
    assert "reclassification" in excluded["offer"]


def test_a_stage_reached_then_superseded_still_counts(client, user_headers, f):
    """Reaching a stage means the event ever arrived, not that it is current -
    an application acknowledged and then rejected belongs in both. That is
    what makes it a funnel rather than a snapshot, and why stages do not sum
    to the total."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    job_id = f.make_job(source="board-b")
    _apply_event(f, uid, job_id, "acknowledgement")
    app = db.query_one("SELECT id FROM applications WHERE job_id = %s", (job_id,))
    msg = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at) VALUES (%s, 'fn-later', 'takeout', 'a@b.com', 's', now()) RETURNING id",
        (uid,),
    )
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s, 'rejection', 'high')",
        (msg["id"],),
    )
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'ats_company', 'high')",
        (msg["id"], app["id"]),
    )

    stages = {
        s["stage"]: s["reached"]
        for s in client.get("/v1/user/funnel", headers=user_headers).json()["overall"]["stages"]
    }
    assert stages["acknowledged"] == 1
    assert stages["rejected"] == 1


def test_per_source_funnel_flags_a_sample_too_small_to_read(client, user_headers, f):
    """The board->outcome number is thin by construction: an application is
    created when a TRACKED posting is marked applied, and that has happened
    for essentially one source. Report the real n, flagged, rather than a
    rate off two rows."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    _apply_event(f, uid, f.make_job(source="tiny-board"), "acknowledgement")

    body = client.get("/v1/user/funnel", headers=user_headers).json()
    row = next(r for r in body["by_source"] if r["source"] == "tiny-board")
    assert row["applications"] == 1
    assert row["below_sample_floor"] is True


def test_criteria_are_served_in_full_shape_whatever_was_saved(client, user_headers):
    """A client reads support for a criterion by the key's presence, so the
    key is present with its default even on a row saved before it existed."""
    from api import db

    fresh = client.get("/v1/user/settings", headers=user_headers).json()["criteria"]
    assert fresh == {"date_posted_after": None, "excluded_locations": [], "included_locations": []}
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (user_headers["X-User-Sub"],))["id"]
    db.execute(
        "INSERT INTO user_settings (user_id, criteria) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET criteria = EXCLUDED.criteria",
        (uid, db.jsonb({"included_terms": [], "excluded_locations": ["UK"]})),
    )
    old_row = client.get("/v1/user/settings", headers=user_headers).json()["criteria"]
    assert old_row == {
        "date_posted_after": None,
        "excluded_locations": ["UK"],
        "included_locations": [],
    }
