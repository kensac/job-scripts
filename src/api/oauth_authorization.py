"""Authenticated OAuth consent state and authorization URLs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from cryptography.fernet import InvalidToken

from api import crypto
from api.oauth_provider import (
    AUTH_ENDPOINT as _AUTH_ENDPOINT,
)
from api.oauth_provider import (
    GMAIL_SCOPES,
    GOOGLE,
    STATE_TTL_SECONDS,
    StateInvalid,
    redirect_uris,
)
from api.oauth_provider import (
    client_id as _client_id,
)


@dataclass(frozen=True)
class OAuthState:
    user_id: int
    provider: str
    redirect_uri: str


def issue_state(user_id: int, provider: str, redirect_uri: str) -> str:
    """Mint a state parameter that the callback can verify without storing it.

    The state is a Fernet token, so it is authenticated and timestamped by the
    same primitive that protects the tokens at rest - no second scheme, and no
    table of pending nonces to expire. It carries no random nonce because
    Fernet already gives every token a random IV; a nonce would add entropy
    nothing reads.

    What makes this safe against an attacker planting their own authorization
    code in someone else's account is not the opacity of the state but the pair
    of checks in verify_state: the payload names the user it was issued to, and
    the callback route is authenticated, so the two must agree. An attacker
    cannot mint a state naming the victim, and cannot reach the callback as the
    victim.
    """
    payload = json.dumps({"user_id": user_id, "provider": provider, "redirect_uri": redirect_uri})
    return crypto.encrypt(payload).decode("ascii")


def verify_state(state: str, *, user_id: int) -> OAuthState:
    try:
        payload = json.loads(crypto.decrypt(state.encode("ascii"), ttl=STATE_TTL_SECONDS))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise StateInvalid("state is not valid or has expired") from exc
    if not isinstance(payload, dict):
        # The same key encrypts user API keys. Nothing exposes that ciphertext,
        # but "it decrypted" must not be allowed to mean "it is a state": a
        # plaintext that happens to parse as a JSON scalar would otherwise
        # reach .get() and raise AttributeError instead of being rejected.
        raise StateInvalid("state is not a state parameter")
    if payload.get("user_id") != user_id:
        raise StateInvalid("state was issued to a different user")
    redirect_uri = payload.get("redirect_uri")
    if redirect_uri not in redirect_uris():
        # The registered set can shrink between issuing and redeeming. Re-check
        # rather than trusting the value we signed, so a URI withdrawn from the
        # client is not still honoured by states already in flight.
        raise StateInvalid("state carries a redirect uri that is no longer registered")
    return OAuthState(
        user_id=user_id, provider=str(payload.get("provider") or GOOGLE), redirect_uri=redirect_uri
    )


def authorization_url(*, user_id: int, redirect_uri: str | None = None) -> str:
    uris = redirect_uris()
    target = redirect_uri or uris[0]
    if target not in uris:
        raise StateInvalid(f"redirect_uri must be one of {uris}")
    params = {
        "client_id": _client_id(),
        "redirect_uri": target,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        # Both are required, for different reasons. Without access_type=offline
        # there is no refresh token at all. With it but without prompt=consent,
        # Google withholds a refresh token on a repeat grant - which is exactly
        # the case here, because the seven-day expiry means every connect after
        # the first is a repeat, and a reconnect that returned only a one-hour
        # access token would leave the workers with nothing to refresh from.
        "access_type": "offline",
        "prompt": "consent",
        "state": issue_state(user_id, GOOGLE, target),
    }
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}"
