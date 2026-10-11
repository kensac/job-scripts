"""Gmail authorization, gate and state behavior."""

from __future__ import annotations

import json
import os
import time
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from api import crypto, db, oauth
from tests.gmail_support import (
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    allow_gmail_connection_for_groups,
    user_id_for_subject,
)
from tests.gmail_support import (
    google as google,
)

# --- the scope, which must never widen -----------------------------------


def test_only_readonly_scope_is_ever_requested(google, admin_headers, client):
    """Every wider Gmail scope permits sending or deleting mail. This asserts
    the constant and the URL built from it, so widening either fails here."""
    assert oauth.GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)
    resp = client.post("/v1/user/gmail/authorize", json={}, headers=admin_headers)
    query = parse_qs(urlparse(resp.json()["authorization_url"]).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_authorization_url_asks_for_a_refresh_token(google, admin_headers, client):
    resp = client.post(
        "/v1/user/gmail/authorize", json={"redirect_uri": REDIRECT_URI}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    parsed = urlparse(resp.json()["authorization_url"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    # Without both of these a reconnect yields no refresh token, and every
    # connect after the first is a reconnect once the seven-day expiry bites.
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert CLIENT_SECRET not in resp.text


def test_unregistered_redirect_uri_is_refused(google, admin_headers, client):
    resp = client.post(
        "/v1/user/gmail/authorize",
        json={"redirect_uri": "https://evil.test/callback"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_REDIRECT_URI"


# --- the feature gate, which must live in config -------------------------


def test_gate_is_closed_to_ordinary_users_by_default(google, user_headers, client):
    status = client.get("/v1/user/gmail/status", headers=user_headers)
    assert status.status_code == 200
    assert status.json()["available"] is False
    assert client.post("/v1/user/gmail/authorize", json={}, headers=user_headers).status_code == 403


def test_gate_opens_by_config_not_by_deploy(google, user_headers, client):
    """The gate has to be a config change, so this proves no group name is
    compiled into the check: the same user is refused, then allowed, with
    nothing but an app_config row changing between the two calls."""
    assert client.post("/v1/user/gmail/authorize", json={}, headers=user_headers).status_code == 403
    allow_gmail_connection_for_groups("jobtracker-users-internal")
    assert client.post("/v1/user/gmail/authorize", json={}, headers=user_headers).status_code == 200
    assert client.get("/v1/user/gmail/status", headers=user_headers).json()["available"] is True


def test_gate_wildcard_opens_it_to_everyone(google, user_headers, client):
    allow_gmail_connection_for_groups(oauth.ALL_GROUPS)
    assert client.get("/v1/user/gmail/status", headers=user_headers).json()["available"] is True


def test_gate_is_editable_through_the_admin_config_endpoint(google, admin_headers, client):
    resp = client.put(
        f"/v1/admin/config/{oauth.CONNECT_GROUPS_KEY}",
        json={"value": ["infra-admins", "jobtracker-users-internal"]},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert oauth.connect_allowed(["jobtracker-users-internal"]) is True


def test_admin_config_rejects_the_wrong_value_shape(admin_headers, client):
    assert (
        client.put(
            f"/v1/admin/config/{oauth.CONNECT_GROUPS_KEY}",
            json={"value": True},
            headers=admin_headers,
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/v1/admin/config/signups_enabled", json={"value": ["yes"]}, headers=admin_headers
        ).status_code
        == 400
    )


def test_malformed_gate_config_denies_rather_than_crashes(google, user_headers, client):
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (oauth.CONNECT_GROUPS_KEY, db.jsonb("infra-admins")),
    )
    assert client.get("/v1/user/gmail/status", headers=user_headers).json()["available"] is False


# --- state, which must be verifiable server-side -------------------------


def test_state_is_rejected_when_replayed_by_another_user(
    google, admin_headers, user_headers, client
):
    """The check that stops an attacker planting their own authorization code
    in someone else's account: the state names the user it was issued to, and
    the callback is authenticated, so the two have to agree."""
    allow_gmail_connection_for_groups("infra-admins", "jobtracker-users-internal")
    issued = client.post("/v1/user/gmail/authorize", json={}, headers=admin_headers)
    state = parse_qs(urlparse(issued.json()["authorization_url"]).query)["state"][0]

    resp = client.post(
        "/v1/user/gmail/callback",
        json={"code": "auth-code", "state": state},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_STATE"
    assert google.token_calls == []


def test_a_non_state_ciphertext_is_rejected(google, admin_headers, client):
    """The same key encrypts users' BYO API keys. Decrypting successfully must
    not be mistaken for being a state parameter."""
    resp = client.post(
        "/v1/user/gmail/callback",
        json={"code": "auth-code", "state": crypto.encrypt("42").decode()},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_STATE"
    assert google.token_calls == []


def test_forged_state_is_rejected(google, admin_headers, client):
    forged = Fernet(Fernet.generate_key()).encrypt(
        json.dumps({"user_id": 1, "provider": "google", "redirect_uri": REDIRECT_URI}).encode()
    )
    resp = client.post(
        "/v1/user/gmail/callback",
        json={"code": "auth-code", "state": forged.decode()},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert google.token_calls == []


def test_expired_state_is_rejected(google, admin_headers, client):
    """Fernet stamps the issue time into the token, so an old state is
    rejected by the same primitive that authenticates it."""
    user_id = user_id_for_subject("test-admin")
    payload = json.dumps(
        {"user_id": user_id, "provider": "google", "redirect_uri": REDIRECT_URI}
    ).encode()
    stale = Fernet(os.environ["APP_ENCRYPTION_KEY"]).encrypt_at_time(
        payload, int(time.time()) - oauth.STATE_TTL_SECONDS - 60
    )
    resp = client.post(
        "/v1/user/gmail/callback",
        json={"code": "auth-code", "state": stale.decode()},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert google.token_calls == []


def test_state_carrying_a_withdrawn_redirect_uri_is_rejected(google, admin_headers, monkeypatch):
    user_id = user_id_for_subject("test-admin")
    state = oauth.issue_state(user_id, oauth.GOOGLE, REDIRECT_URI)
    monkeypatch.setenv("GMAIL_OAUTH_REDIRECT_URIS", "https://www.kanishksachdev.com/x/callback")
    with pytest.raises(oauth.StateInvalid):
        oauth.verify_state(state, user_id=user_id)
