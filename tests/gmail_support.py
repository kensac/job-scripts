"""Gmail connect: the credential half of mail ingest.

Everything below runs against the real database - the row lock in
get_access_token is the point of several of these tests and a mock cannot have
one. Only the boundary to Google is faked, because the alternative is a test
suite that needs a live OAuth grant.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from api import db, oauth

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


def user_id_for_subject(sub: str) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (sub,))
    assert row is not None
    return row["id"]


def allow_gmail_connection_for_groups(*groups: str) -> None:
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (oauth.CONNECT_GROUPS_KEY, db.jsonb(list(groups))),
    )


def connect_gmail_account(client, headers, google: FakeGoogle) -> dict:
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


def expire_access_token(user_id: int) -> None:
    db.execute(
        "UPDATE user_oauth_tokens SET access_token_expires_at = now() - interval '1 hour' "
        "WHERE user_id = %s",
        (user_id,),
    )
