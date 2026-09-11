from api import crypto, db


def test_missing_model_has_same_reason_in_filter_admission_and_ai_errors(
    client, user_headers, runs_permitted
):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    db.execute(
        "UPDATE user_settings SET api_key_enc = %s, ai_provider = 'openai_compatible', ai_model = NULL "
        "WHERE user_id = %s",
        (crypto.encrypt("test-key"), uid),
    )
    row = client.post(
        "/v1/user/filters", json={"name": "test", "prompt": "remote"}, headers=user_headers
    ).json()
    settings = client.get("/v1/user/settings", headers=user_headers).json()
    assert settings["unavailable_reason"] == "LookupError"
    assert settings["unavailable_code"] == "NO_MODEL"
    assert settings["unavailable_message"]
    listed = client.get("/v1/user/filters", headers=user_headers).json()
    for decision in (listed["filters"][0]["run_admission"], listed["run_all_admission"]):
        assert decision["allowed"] is False
        assert decision["reason"] == "NO_MODEL"
        assert decision["message"]
    for path, body in (
        (f"/v1/user/filters/{row['id']}/run", None),
        ("/v1/user/filters/run-all", None),
        ("/v1/ai/improve-prompt", {"prompt": "remote"}),
        (
            "/v1/user/apply/suggest",
            {"fields": [{"key": "why", "label": "Why us?", "kind": "text"}]},
        ),
    ):
        response = client.post(path, json=body, headers=user_headers)
        assert response.status_code == 402
        assert response.json()["detail"]["code"] == "NO_MODEL"
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind LIKE 'run%'")["n"] == 0


def test_filter_admission_refuses_missing_owner_provider_key(
    client, user_headers, monkeypatch, runs_permitted
):
    from api import ai

    monkeypatch.setattr(ai, "server_key", lambda provider: "")
    listed = client.get("/v1/user/filters", headers=user_headers).json()
    assert listed["run_all_admission"]["allowed"] is False
    assert listed["run_all_admission"]["reason"] == "NO_API_KEY"
    response = client.post("/v1/user/filters/run-all", headers=user_headers)
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "NO_API_KEY"


def test_missing_model_refuses_draft_before_any_task_is_queued(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    job_id = f.make_job()
    f.make_board_row(uid, job_id, status="Saved")
    resume = client.post(
        "/v1/user/resumes",
        json={"name": "main", "text": "Python experience."},
        headers=user_headers,
    )
    assert resume.status_code == 201
    db.execute(
        "UPDATE user_settings SET api_key_enc = %s, ai_provider = 'openai_compatible', "
        "ai_model = NULL WHERE user_id = %s",
        (crypto.encrypt("test-key"), uid),
    )
    response = client.post(
        f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers
    )
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "NO_MODEL"
    assert (
        db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'application_draft'")["n"] == 0
    )
