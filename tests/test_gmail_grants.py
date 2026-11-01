"""Gmail grant exchange, refresh and health behavior."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest

from api import crypto, db, health, oauth
from tests.factories import user_id_for_subject
from tests.gmail_support import (
    CLIENT_SECRET,
    FakeResponse,
    connect_gmail_account,
    expire_access_token,
)
from tests.gmail_support import google as google

# --- the exchange --------------------------------------------------------


def test_connect_stores_an_encrypted_grant(google, admin_headers, client):
    body = connect_gmail_account(client, admin_headers, google)
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
        (user_id_for_subject("test-admin"),),
    )
    assert row is not None
    assert b"refresh-1" not in bytes(row["refresh_token_enc"])
    assert crypto.decrypt(row["refresh_token_enc"]) == "refresh-1"
    assert crypto.decrypt(row["access_token_enc"]) == "access-1"
    assert row["invalid_at"] is None


def test_the_client_secret_never_reaches_the_caller(google, admin_headers, client):
    body = connect_gmail_account(client, admin_headers, google)
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
    body = connect_gmail_account(client, admin_headers, google)
    assert body["connected"] is True
    assert body["account_email"] is None


# --- refresh -------------------------------------------------------------


def test_a_live_access_token_is_reused_without_calling_the_provider(google, admin_headers, client):
    connect_gmail_account(client, admin_headers, google)
    before = len(google.token_calls)
    assert oauth.get_access_token(user_id_for_subject("test-admin")) == "access-1"
    assert len(google.token_calls) == before


def test_an_expired_access_token_is_refreshed_and_stored(google, admin_headers, client):
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
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
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
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
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
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
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
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
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
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
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
    google.token_response = FakeResponse(503, {"error": "backend_error"})
    with pytest.raises(oauth.ProviderError):
        oauth.get_access_token(user_id)
    row = db.query_one("SELECT invalid_at FROM user_oauth_tokens WHERE user_id = %s", (user_id,))
    assert row is not None and row["invalid_at"] is None


def test_the_dead_grant_is_surfaced_to_the_user(google, admin_headers, client):
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
    google.token_response = FakeResponse(400, {"error": "invalid_grant"})
    with pytest.raises(oauth.NeedsReconnect):
        oauth.get_access_token(user_id)

    body = client.get("/v1/user/gmail/status", headers=admin_headers).json()
    assert body["connected"] is True
    assert body["needs_reconnect"] is True
    assert body["invalid_reason"] == "invalid_grant"


def test_the_dead_grant_raises_a_health_alert(google, admin_headers, client):
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
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
    connect_gmail_account(client, admin_headers, google)
    user_id = user_id_for_subject("test-admin")
    expire_access_token(user_id)
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
    body = connect_gmail_account(client, admin_headers, google)
    assert body["needs_reconnect"] is False
    assert oauth.get_access_token(user_id) == "access-3"
    assert not [f for f in health.detect() if f["subject"] == f"google:{user_id}"]


def test_healthy_grants_raise_no_alert(google, admin_headers, client):
    connect_gmail_account(client, admin_headers, google)
    assert not [f for f in health.detect() if f["kind"] == "oauth_token_invalid"]
