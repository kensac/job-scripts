"""Gmail disconnection and owner isolation behavior."""

from __future__ import annotations

import pytest
import requests

from api import db, oauth
from tests.gmail_support import (
    allow_gmail_connection_for_groups,
    connect_gmail_account,
    user_id_for_subject,
)
from tests.gmail_support import (
    google as google,
)

# --- disconnect ----------------------------------------------------------


def test_disconnect_deletes_the_grant_and_revokes_it(google, admin_headers, client):
    connect_gmail_account(client, admin_headers, google)
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
    connect_gmail_account(client, admin_headers, google)
    allow_gmail_connection_for_groups()
    assert (
        client.post("/v1/user/gmail/authorize", json={}, headers=admin_headers).status_code == 403
    )
    assert client.delete("/v1/user/gmail", headers=admin_headers).json() == {"ok": True}


def test_a_failed_revoke_still_deletes_the_row(google, admin_headers, client, monkeypatch):
    connect_gmail_account(client, admin_headers, google)

    def explode(*args, **kwargs):
        raise requests.ConnectionError("revoke endpoint down")

    monkeypatch.setattr(google, "post", explode)
    assert client.delete("/v1/user/gmail", headers=admin_headers).json() == {"ok": True}
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None


# --- isolation between users ---------------------------------------------


def test_one_user_cannot_see_or_delete_another_users_grant(
    google, admin_headers, other_user_headers, client
):
    connect_gmail_account(client, admin_headers, google)
    assert client.get("/v1/user/gmail/status", headers=other_user_headers).json()["connected"] is (
        False
    )
    assert client.delete("/v1/user/gmail", headers=other_user_headers).json() == {"ok": False}
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is not None


def test_deleting_a_user_deletes_the_grant(google, admin_headers, client):
    connect_gmail_account(client, admin_headers, google)
    db.execute("DELETE FROM users WHERE id = %s", (user_id_for_subject("test-admin"),))
    assert db.query_one("SELECT 1 AS x FROM user_oauth_tokens") is None


def test_an_unconnected_user_gets_not_connected_not_a_crash(google, admin_headers):
    with pytest.raises(oauth.NotConnected):
        oauth.get_access_token(user_id_for_subject("test-admin"))
