from copy import deepcopy

import pytest

from api import db

PATH = "/v1/extension/config?schema_version=1&adapter=ashby"
ADMIN_PATH = "/v1/admin/config/extension_policy"


def test_policy_is_typed_and_does_not_create_a_fill(client, user_headers):
    response = client.get(PATH, headers=user_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["adapter"] == "ashby"
    assert len(body["revision"]) == 64
    assert response.headers["cache-control"] == "no-store"
    assert all(flag == {"allowed": True, "reason": None} for flag in body["features"].values())
    assert db.query_one("SELECT count(*) AS n FROM application_fills")["n"] == 0


def test_atomic_controls_and_rollback(client, user_headers, admin_headers):
    original = client.get(PATH, headers=user_headers).json()
    policy = {"features": {"resume_upload": False}, "max_age_seconds": 60}
    response = client.put(ADMIN_PATH, headers=admin_headers, json={"value": policy})
    assert response.status_code == 200
    disabled = client.get(PATH, headers=user_headers).json()
    assert disabled["revision"] != original["revision"]
    assert disabled["max_age_seconds"] == 60
    assert disabled["features"]["resume_upload"] == {"allowed": False, "reason": "FEATURE_DISABLED"}
    assert disabled["features"]["autofill"]["allowed"] is True
    assert client.put(ADMIN_PATH, headers=admin_headers, json={"value": {}}).status_code == 200
    assert client.get(PATH, headers=user_headers).json() == original


def test_adapter_disable_and_global_kill_switch(client, user_headers, admin_headers):
    for policy in ({"disabled_adapters": ["ashby"]}, {"features": {"autofill": False}}):
        assert (
            client.put(ADMIN_PATH, headers=admin_headers, json={"value": policy}).status_code == 200
        )
        body = client.get(PATH, headers=user_headers).json()
        assert all(not flag["allowed"] for flag in body["features"].values())
    client.put(ADMIN_PATH, headers=admin_headers, json={"value": {"disabled_adapters": ["lever"]}})
    assert all(
        flag["allowed"]
        for flag in client.get(PATH, headers=user_headers).json()["features"].values()
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"actions": [{"click": "submit"}]},
        {"features": {"eval": "alert(1)"}},
        {"features": {"autofill": "false"}},
        {"features": {"autofill": 0}},
        {"disabled_adapters": ["https://example.com"]},
        {"max_age_seconds": -1},
        {"max_age_seconds": 86401},
    ],
)
def test_invalid_policy_cannot_replace_current_policy(client, user_headers, admin_headers, bad):
    before = client.get(PATH, headers=user_headers).json()
    response = client.put(ADMIN_PATH, headers=admin_headers, json={"value": bad})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_VALUE"
    assert client.get(PATH, headers=user_headers).json() == before


def test_corrupt_persisted_policy_is_unavailable_not_enabled(client, user_headers):
    db.execute(
        "UPDATE app_config SET value = %s WHERE key = 'extension_policy'",
        (db.jsonb({"actions": []}),),
    )
    response = client.get(PATH, headers=user_headers)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "INVALID_EXTENSION_POLICY"


def test_version_negotiation_and_authorization(client, user_headers):
    assert client.get(PATH).status_code == 200
    response = client.get(PATH.replace("version=1", "version=2"), headers=user_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "UNSUPPORTED_CONFIG_SCHEMA"
    assert client.put(ADMIN_PATH, headers=user_headers, json={"value": {}}).status_code == 403


def test_policy_revision_is_semantic(client, user_headers, admin_headers):
    policy = {"disabled_adapters": ["lever", "workday"]}
    assert client.put(ADMIN_PATH, headers=admin_headers, json={"value": policy}).status_code == 200
    response = client.get(PATH, headers=user_headers)
    assert response.status_code == 200
    original = response.json()
    reordered = deepcopy(policy)
    reordered["disabled_adapters"].reverse()
    assert (
        client.put(ADMIN_PATH, headers=admin_headers, json={"value": reordered}).status_code == 200
    )
    assert client.get(PATH, headers=user_headers).json() == original


def test_public_config_never_provisions_a_user(client):
    before = db.query_one("SELECT count(*) AS n FROM users")["n"]
    response = client.get(PATH)
    assert response.status_code == 200
    assert db.query_one("SELECT count(*) AS n FROM users")["n"] == before
