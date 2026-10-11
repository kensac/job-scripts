from __future__ import annotations

import os

SERVICE_TOKEN = os.environ["JOBTRACKER_SERVICE_TOKEN"]


def test_missing_service_token_is_401(client):
    resp = client.get("/v1/user/jobs")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


def test_wrong_service_token_is_401(client):
    resp = client.get("/v1/user/jobs", headers={"X-Service-Token": "definitely-wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


def test_valid_token_missing_user_headers_is_401(client):
    resp = client.get("/v1/user/jobs", headers={"X-Service-Token": SERVICE_TOKEN})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"
