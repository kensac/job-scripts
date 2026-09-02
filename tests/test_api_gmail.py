"""Gmail connect: the credential half of mail ingest.

Everything below runs against the real database - the row lock in
get_access_token is the point of several of these tests and a mock cannot have
one. Only the boundary to Google is faked, because the alternative is a test
suite that needs a live OAuth grant.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from cryptography.fernet import Fernet

from api import crypto, db, health, oauth

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "http://localhost:3000/job-tracker/settings/gmail/callback"


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeGoogle:
    """Stands in for the module-level `requests` inside api.oauth.

    Replacing the name in oauth's own namespace rather than patching the
    requests package keeps the fake from leaking into events.py, which posts to
    Centrifugo through the same library.
    """

    RequestException = requests.RequestException

    def __init__(self) -> None:
        self.token_calls: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.profile_calls = 0
        self.token_response = FakeResponse(
            200,
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "scope": " ".join(oauth.GMAIL_SCOPES),
                "token_type": "Bearer",
            },
        )
        self.profile_response = FakeResponse(200, {"emailAddress": "kanishk@example.test"})
        self.on_token: Any = None

    def post(self, url: str, data: dict | None = None, timeout: int | None = None):
        data = data or {}
        if url.endswith("/revoke"):
            self.revoked.append(data.get("token", ""))
            return FakeResponse(200, {})
        self.token_calls.append(data)
        if self.on_token is not None:
            self.on_token(data)
        return self.token_response

    def get(self, url: str, headers: dict | None = None, timeout: int | None = None):
        self.profile_calls += 1
        return self.profile_response

    @property
    def refresh_calls(self) -> list[dict[str, str]]:
        return [c for c in self.token_calls if c.get("grant_type") == "refresh_token"]


@pytest.fixture
def google(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(oauth, "requests", fake)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", CLIENT_SECRET)
    return fake


def _user_id(sub: str) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (sub,))
    assert row is not None
    return row["id"]


def _open_gate(*groups: str) -> None:
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (oauth.CONNECT_GROUPS_KEY, db.jsonb(list(groups))),
    )


def _connect(client, headers, google: FakeGoogle) -> dict:
    """Drive a real connect through the API, so the tests that need a stored
    grant get one the same way production does."""
    state = client.post("/v1/user/gmail/authorize", json={}, headers=headers)
    assert state.status_code == 200, state.text
    query = parse_qs(urlparse(state.json()["authorization_url"]).query)
    resp = client.post(
        "/v1/user/gmail/callback",
        json={"code": "auth-code", "state": query["state"][0]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _expire_access_token(user_id: int) -> None:
    db.execute(
        "UPDATE user_oauth_tokens SET access_token_expires_at = now() - interval '1 hour' "
        "WHERE user_id = %s",
        (user_id,),
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
    _open_gate("jobtracker-users-internal")
    assert client.post("/v1/user/gmail/authorize", json={}, headers=user_headers).status_code == 200
    assert client.get("/v1/user/gmail/status", headers=user_headers).json()["available"] is True


def test_gate_wildcard_opens_it_to_everyone(google, user_headers, client):
    _open_gate(oauth.ALL_GROUPS)
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
    _open_gate("infra-admins", "jobtracker-users-internal")
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
    user_id = _user_id("test-admin")
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
    user_id = _user_id("test-admin")
    state = oauth.issue_state(user_id, oauth.GOOGLE, REDIRECT_URI)
    monkeypatch.setenv("GMAIL_OAUTH_REDIRECT_URIS", "https://www.kanishksachdev.com/x/callback")
    with pytest.raises(oauth.StateInvalid):
        oauth.verify_state(state, user_id=user_id)


# --- the exchange --------------------------------------------------------


def test_connect_stores_an_encrypted_grant(google, admin_headers, client):
    body = _connect(client, admin_headers, google)
    assert body["connected"] is True
    assert body["needs_reconnect"] is False
    assert body["account_email"] == "kanishk@example.test"
    assert body["scopes"] == list(oauth.GMAIL_SCOPES)

    exchange = google.token_calls[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["client_secret"] == CLIENT_SECRET
    assert exchange["redirect_uri"] in oauth.redirect_uris()

    row = db.query_one(
        "SELECT refresh_token_enc, access_token_enc, account_email, scopes, invalid_at "
        "FROM user_oauth_tokens WHERE user_id = %s",
        (_user_id("test-admin"),),
    )
    assert row is not None
    assert b"refresh-1" not in bytes(row["refresh_token_enc"])
    assert crypto.decrypt(row["refresh_token_enc"]) == "refresh-1"
    assert crypto.decrypt(row["access_token_enc"]) == "access-1"
    assert row["invalid_at"] is None


def test_the_client_secret_never_reaches_the_caller(google, admin_headers, client):
    body = _connect(client, admin_headers, google)
    assert CLIENT_SECRET not in json.dumps(body)


def test_a_declined_scope_stores_nothing(google, admin_headers, client):
    google.token_response = FakeResponse(
        200,
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "openid"},
    )
    issued = client.post("/v1/user/gmail/authorize", json={}, headers=admin_headers)
    state = parse_qs(urlparse(issued.json()["authorization_url"]).query)["state"][0]
    resp = client.post(
        "/v1/user/gmail/callback", json={"code": "c", "state": state}, headers=admin_headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "SCOPE_DECLINED"
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None


def test_a_grant_without_a_refresh_token_is_refused(google, admin_headers, client):
    """An access token alone expires in an hour with nothing to renew it from,
    so a worker would find the connection dead and unrecoverable."""
    google.token_response = FakeResponse(
        200,
        {"access_token": "a", "expires_in": 3600, "scope": " ".join(oauth.GMAIL_SCOPES)},
    )
    issued = client.post("/v1/user/gmail/authorize", json={}, headers=admin_headers)
    state = parse_qs(urlparse(issued.json()["authorization_url"]).query)["state"][0]
    resp = client.post(
        "/v1/user/gmail/callback", json={"code": "c", "state": state}, headers=admin_headers
    )
    assert resp.status_code == 502
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None


def test_a_failed_exchange_does_not_report_success(google, admin_headers, client):
    google.token_response = FakeResponse(400, {"error": "invalid_grant"})
    issued = client.post("/v1/user/gmail/authorize", json={}, headers=admin_headers)
    state = parse_qs(urlparse(issued.json()["authorization_url"]).query)["state"][0]
    resp = client.post(
        "/v1/user/gmail/callback", json={"code": "c", "state": state}, headers=admin_headers
    )
    assert resp.status_code == 502
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None


def test_a_profile_lookup_failure_does_not_fail_the_connect(google, admin_headers, client):
    google.profile_response = FakeResponse(500, {})
    body = _connect(client, admin_headers, google)
    assert body["connected"] is True
    assert body["account_email"] is None


# --- refresh -------------------------------------------------------------


def test_a_live_access_token_is_reused_without_calling_the_provider(google, admin_headers, client):
    _connect(client, admin_headers, google)
    before = len(google.token_calls)
    assert oauth.get_access_token(_user_id("test-admin")) == "access-1"
    assert len(google.token_calls) == before


def test_an_expired_access_token_is_refreshed_and_stored(google, admin_headers, client):
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(
        200, {"access_token": "access-2", "expires_in": 3600, "scope": " ".join(oauth.GMAIL_SCOPES)}
    )

    assert oauth.get_access_token(user_id) == "access-2"
    assert google.refresh_calls[0]["refresh_token"] == "refresh-1"
    row = db.query_one(
        "SELECT access_token_enc, refresh_token_enc FROM user_oauth_tokens WHERE user_id = %s",
        (user_id,),
    )
    assert row is not None
    assert crypto.decrypt(row["access_token_enc"]) == "access-2"
    # Google does not rotate on this flow; the stored refresh token must
    # survive a response that carries none rather than being nulled out.
    assert crypto.decrypt(row["refresh_token_enc"]) == "refresh-1"
    # The second call is served from the row the first one wrote.
    assert oauth.get_access_token(user_id) == "access-2"
    assert len(google.refresh_calls) == 1


def test_a_rotated_refresh_token_is_persisted(google, admin_headers, client):
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(
        200,
        {
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "expires_in": 3600,
            "scope": " ".join(oauth.GMAIL_SCOPES),
        },
    )
    oauth.get_access_token(user_id)
    row = db.query_one(
        "SELECT refresh_token_enc FROM user_oauth_tokens WHERE user_id = %s", (user_id,)
    )
    assert row is not None
    assert crypto.decrypt(row["refresh_token_enc"]) == "refresh-2"


def test_concurrent_refreshes_hit_the_provider_once(google, admin_headers, client):
    """Three hosts run ingest, so the same user's token gets refreshed from
    several processes at once. get_access_token holds a row lock across the
    whole read-decide-refresh-write, so the loser of the race must find the
    winner's token already stored rather than perform a second refresh - which
    matters because Google caps live refresh tokens per user and drops the
    oldest past the cap.

    Two real threads on two real connections; the lock under test is in
    Postgres, not in this process."""
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(
        200, {"access_token": "access-2", "expires_in": 3600, "scope": " ".join(oauth.GMAIL_SCOPES)}
    )
    # Widen the window the loser has to contend for, so the test exercises the
    # lock rather than the two calls happening to be sequential.
    google.on_token = lambda data: time.sleep(0.3)

    start = threading.Barrier(2)

    def refresh() -> str:
        start.wait(timeout=5)
        return oauth.get_access_token(user_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=30) for f in [pool.submit(refresh), pool.submit(refresh)]]

    assert results == ["access-2", "access-2"]
    assert len(google.refresh_calls) == 1


# --- the dead grant, which must be loud ----------------------------------


def test_a_rejected_refresh_token_is_recorded_durably(google, admin_headers, client):
    """The invalidation is written from inside the connection block that then
    raises. Without an explicit commit the rollback on the way out would
    discard it, and the system would be back to failing silently."""
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(400, {"error": "invalid_grant"})

    with pytest.raises(oauth.NeedsReconnect):
        oauth.get_access_token(user_id)

    row = db.query_one(
        "SELECT invalid_at, invalid_reason, access_token_enc FROM user_oauth_tokens "
        "WHERE user_id = %s",
        (user_id,),
    )
    assert row is not None
    assert row["invalid_at"] is not None
    assert row["invalid_reason"] == "invalid_grant"
    assert row["access_token_enc"] is None


def test_a_dead_grant_fails_fast_without_retrying_the_provider(google, admin_headers, client):
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(400, {"error": "invalid_grant"})
    with pytest.raises(oauth.NeedsReconnect):
        oauth.get_access_token(user_id)
    before = len(google.token_calls)
    with pytest.raises(oauth.NeedsReconnect):
        oauth.get_access_token(user_id)
    assert len(google.token_calls) == before


def test_a_transient_provider_failure_is_not_a_reconnect(google, admin_headers, client):
    """A 500 from Google, or our own client credentials being wrong, must not
    be laundered into a 'reconnect your mailbox' prompt aimed at the user."""
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(503, {"error": "backend_error"})
    with pytest.raises(oauth.ProviderError):
        oauth.get_access_token(user_id)
    row = db.query_one("SELECT invalid_at FROM user_oauth_tokens WHERE user_id = %s", (user_id,))
    assert row is not None and row["invalid_at"] is None


def test_the_dead_grant_is_surfaced_to_the_user(google, admin_headers, client):
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(400, {"error": "invalid_grant"})
    with pytest.raises(oauth.NeedsReconnect):
        oauth.get_access_token(user_id)

    body = client.get("/v1/user/gmail/status", headers=admin_headers).json()
    assert body["connected"] is True
    assert body["needs_reconnect"] is True
    assert body["invalid_reason"] == "invalid_grant"


def test_the_dead_grant_raises_a_health_alert(google, admin_headers, client):
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(400, {"error": "invalid_grant"})
    with pytest.raises(oauth.NeedsReconnect):
        oauth.get_access_token(user_id)

    subject = f"google:{user_id}"
    found = [f for f in health.detect() if f["subject"] == subject]
    assert len(found) == 1
    assert found[0]["kind"] == "oauth_token_invalid"
    assert "kanishk@example.test" in found[0]["message"]
    assert [f["id"] for f in health.record(found) if f["subject"] == subject]


def test_reconnecting_clears_the_dead_state(google, admin_headers, client):
    _connect(client, admin_headers, google)
    user_id = _user_id("test-admin")
    _expire_access_token(user_id)
    google.token_response = FakeResponse(400, {"error": "invalid_grant"})
    with pytest.raises(oauth.NeedsReconnect):
        oauth.get_access_token(user_id)

    google.token_response = FakeResponse(
        200,
        {
            "access_token": "access-3",
            "refresh_token": "refresh-3",
            "expires_in": 3600,
            "scope": " ".join(oauth.GMAIL_SCOPES),
        },
    )
    body = _connect(client, admin_headers, google)
    assert body["needs_reconnect"] is False
    assert oauth.get_access_token(user_id) == "access-3"
    assert not [f for f in health.detect() if f["subject"] == f"google:{user_id}"]


def test_healthy_grants_raise_no_alert(google, admin_headers, client):
    _connect(client, admin_headers, google)
    assert not [f for f in health.detect() if f["kind"] == "oauth_token_invalid"]


# --- disconnect ----------------------------------------------------------


def test_disconnect_deletes_the_grant_and_revokes_it(google, admin_headers, client):
    _connect(client, admin_headers, google)
    resp = client.delete("/v1/user/gmail", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None
    assert google.revoked == ["refresh-1"]
    assert client.get("/v1/user/gmail/status", headers=admin_headers).json()["connected"] is False


def test_disconnect_is_idempotent(google, admin_headers, client):
    assert client.delete("/v1/user/gmail", headers=admin_headers).json() == {"ok": False}


def test_disconnect_survives_the_feature_being_closed_off(google, admin_headers, client):
    """Someone who connected before the gate narrowed must still be able to
    revoke, so this route is gated on being signed in and nothing else."""
    _connect(client, admin_headers, google)
    _open_gate()
    assert (
        client.post("/v1/user/gmail/authorize", json={}, headers=admin_headers).status_code == 403
    )
    assert client.delete("/v1/user/gmail", headers=admin_headers).json() == {"ok": True}


def test_a_failed_revoke_still_deletes_the_row(google, admin_headers, client, monkeypatch):
    _connect(client, admin_headers, google)

    def explode(*args, **kwargs):
        raise requests.ConnectionError("revoke endpoint down")

    monkeypatch.setattr(google, "post", explode)
    assert client.delete("/v1/user/gmail", headers=admin_headers).json() == {"ok": True}
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None


# --- isolation between users ---------------------------------------------


def test_one_user_cannot_see_or_delete_another_users_grant(
    google, admin_headers, other_user_headers, client
):
    _connect(client, admin_headers, google)
    assert client.get("/v1/user/gmail/status", headers=other_user_headers).json()["connected"] is (
        False
    )
    assert client.delete("/v1/user/gmail", headers=other_user_headers).json() == {"ok": False}
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is not None


def test_deleting_a_user_deletes_the_grant(google, admin_headers, client):
    _connect(client, admin_headers, google)
    db.execute("DELETE FROM users WHERE id = %s", (_user_id("test-admin"),))
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None


def test_an_unconnected_user_gets_not_connected_not_a_crash(google, admin_headers):
    with pytest.raises(oauth.NotConnected):
        oauth.get_access_token(_user_id("test-admin"))
