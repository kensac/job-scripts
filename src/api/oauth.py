"""Per-user OAuth credentials for external providers, and the token refresh
the workers call during ingest.

Google is the only provider today, but the storage is keyed on (user_id,
provider) because the current grant is known to be temporary: the OAuth client
is in Testing mode with a restricted scope, so Google expires its refresh
tokens after seven days. The two documented escapes - a verified production
app, or IMAP with an app password - are both provider swaps, and this module
exists so that neither is a rewrite.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.fernet import InvalidToken
from psycopg import Connection

from api import crypto, db

logger = logging.getLogger("jobtracker_api")

GOOGLE = "google"

# Read-only, and deliberately nothing else. Every wider Gmail scope carries the
# ability to send, modify or delete mail, which no part of this system has any
# business holding. This list is asserted in the test suite so widening it
# cannot happen quietly.
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
_PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# The redirect URIs registered on the OAuth client. This is not a preference
# list: Google compares the redirect byte-for-byte, so an entry here that is
# not also in the Google console fails at the consent screen with
# redirect_uri_mismatch. Vercel preview deployments therefore cannot be
# supported at all - their hostnames are minted per deploy.
_REGISTERED_REDIRECT_URIS = (
    "https://www.kanishksachdev.com/job-tracker/settings/gmail/callback",
    "http://localhost:3000/job-tracker/settings/gmail/callback",
)

# How long an issued state parameter stays redeemable. It has to cover a person
# reading Google's consent screen and choosing an account. It does not need to
# be longer, because Google's authorization codes themselves expire after ten
# minutes: a wider window here could only ever admit a state whose code is
# already dead.
STATE_TTL_SECONDS = 600

# Treat an access token as spent this far before its nominal expiry. An ingest
# run holds one token across a long series of Gmail calls, so the margin has to
# cover the run drifting past the boundary mid-flight, not just one round trip.
_EXPIRY_SKEW = "2 minutes"

# Every call to Google is bounded by this. It is not a nicety: get_access_token
# holds a Postgres row lock across the refresh request, so this timeout is
# exactly the bound on how long one stuck request can stall every other worker
# waiting for the same user's token.
_HTTP_TIMEOUT = 10

# The one token-endpoint error that means "this grant is gone, the user must
# consent again" - revoked, expired, or the seven-day Testing-mode death.
# invalid_client and the rest are operator faults and must not be laundered
# into a reconnect prompt aimed at the user.
_DEAD_GRANT_ERROR = "invalid_grant"

# app_config key holding the groups allowed to connect a mailbox, as a JSON
# list. Seeded to infra-admins. Opening the feature up is an edit through
# PUT /v1/admin/config/{key}, not a deploy - which is the whole reason this is
# not the hardcoded group set that routers/admin.py uses.
CONNECT_GROUPS_KEY = "gmail_connect_groups"

# Sentinel inside that list meaning "any signed-in user", so the feature can be
# opened to everyone without inventing a second config key whose interaction
# with the first has to be reasoned about.
ALL_GROUPS = "*"


class OAuthError(Exception):
    """Base for every failure in this module that a route needs to distinguish."""


class NotConnected(OAuthError):
    """No stored grant for this (user, provider)."""


class NeedsReconnect(OAuthError):
    """The provider rejected the stored refresh token.

    This is the loud failure the whole feature turns on. A mail ingest that
    quietly stops finding mail is indistinguishable from a quiet week, so every
    path that discovers a dead grant must surface it rather than degrade.
    """


class StateInvalid(OAuthError):
    """The state parameter was forged, expired, or issued to another user."""


class ScopeDeclined(OAuthError):
    """The user completed consent without granting the scope we need."""


class ProviderError(OAuthError):
    """The provider's endpoint failed in a way that is not the user's problem."""


def _required_env(name: str) -> str:
    """A worker without the credential is a misconfigured host, not a dead grant.

    os.environ[...] raised a bare KeyError whose whole message was the variable
    name, and it surfaced on the one task whose stated purpose is to notice a
    revoked token. transmission and the laptop checkout both failed this way on
    2026-09-04 while every container host passed, so the alarm built to tell
    "your mail stopped" from "a quiet week" was reporting neither.

    ProviderError, deliberately: this is not the user's problem and must never
    be read as NeedsReconnect.
    """
    try:
        return os.environ[name]
    except KeyError:
        raise ProviderError(
            f"{name} is not set on worker {os.environ.get('JOBTRACKER_WORKER_NAME', '?')}; "
            "the grant is fine, this host cannot refresh it"
        ) from None


def _client_id() -> str:
    return _required_env("GOOGLE_OAUTH_CLIENT_ID")


def _client_secret() -> str:
    # Read at call time rather than bound at import: the value is injected from
    # Infisical, and a module-level constant would freeze whatever was present
    # when the process started. It never leaves this module.
    return _required_env("GOOGLE_OAUTH_CLIENT_SECRET")


def redirect_uris() -> list[str]:
    raw = os.environ.get("GMAIL_OAUTH_REDIRECT_URIS", "")
    return [u.strip() for u in raw.split(",") if u.strip()] or list(_REGISTERED_REDIRECT_URIS)


def connect_allowed(groups: list[str]) -> bool:
    allowed = db.get_config(CONNECT_GROUPS_KEY, [])
    if not isinstance(allowed, list):
        logger.warning("%s is not a list, refusing access", CONNECT_GROUPS_KEY)
        return False
    return ALL_GROUPS in allowed or bool(set(allowed) & set(groups))


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


def _error_code(resp: requests.Response) -> str:
    """The provider's machine-readable error, never its body.

    A token-endpoint response body holds tokens on success and can echo the
    request on failure, so nothing here ever returns or logs the body itself.
    """
    try:
        payload = resp.json()
    except ValueError:
        return ""
    return str(payload.get("error") or "") if isinstance(payload, dict) else ""


def _post_token(data: dict[str, str]) -> requests.Response:
    try:
        return requests.post(_TOKEN_ENDPOINT, data=data, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError(f"token endpoint unreachable: {type(exc).__name__}") from exc


def _granted_scopes(payload: dict[str, Any]) -> list[str]:
    return [s for s in str(payload.get("scope") or "").split(" ") if s]


def _account_email(access_token: str) -> str | None:
    """The connected mailbox's address, via the one read call gmail.readonly
    allows. Best effort: a connection that works is not worth failing because
    the label for it could not be fetched."""
    try:
        resp = requests.get(
            _PROFILE_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning("gmail profile lookup failed with %s", resp.status_code)
            return None
        payload = resp.json()
    except (requests.RequestException, ValueError):
        logger.warning("gmail profile lookup failed", exc_info=True)
        return None
    return payload.get("emailAddress") if isinstance(payload, dict) else None


def exchange_code(*, user_id: int, code: str, state: str) -> None:
    """Trade an authorization code for tokens and store the grant.

    The exchange lives here rather than in the frontend because it needs the
    client secret, and the secret must never reach a Vercel bundle.
    """
    verified = verify_state(state, user_id=user_id)
    resp = _post_token(
        {
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": verified.redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    if resp.status_code >= 400:
        raise ProviderError(f"code exchange failed: {_error_code(resp) or resp.status_code}")
    payload = resp.json()

    granted = _granted_scopes(payload)
    missing = [s for s in GMAIL_SCOPES if s not in granted]
    if missing:
        # Google returns a usable token even when the user unticks a scope on
        # the consent screen. Storing that grant would produce a connection
        # that looks healthy and fails on the first ingest call.
        raise ScopeDeclined(f"consent did not grant {', '.join(missing)}")

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        # prompt=consent is meant to guarantee one. Without it the grant is
        # unusable from a worker, so refuse rather than store a credential that
        # dies in an hour with nothing to renew it from.
        raise ProviderError("provider returned no refresh token")

    access_token = payload.get("access_token") or ""
    db.execute(
        """
        INSERT INTO user_oauth_tokens
            (user_id, provider, refresh_token_enc, access_token_enc,
             access_token_expires_at, scopes, account_email)
        VALUES (%(user_id)s, %(provider)s, %(refresh)s, %(access)s,
                now() + make_interval(secs => %(expires_in)s), %(scopes)s, %(email)s)
        ON CONFLICT (user_id, provider) DO UPDATE SET
            refresh_token_enc = EXCLUDED.refresh_token_enc,
            access_token_enc = EXCLUDED.access_token_enc,
            access_token_expires_at = EXCLUDED.access_token_expires_at,
            scopes = EXCLUDED.scopes,
            account_email = EXCLUDED.account_email,
            invalid_at = NULL,
            invalid_reason = NULL,
            connected_at = now(),
            updated_at = now()
        """,
        {
            "user_id": user_id,
            "provider": verified.provider,
            "refresh": crypto.encrypt(refresh_token),
            "access": crypto.encrypt(access_token) if access_token else None,
            "expires_in": float(payload.get("expires_in") or 0),
            "scopes": granted,
            "email": _account_email(access_token) if access_token else None,
        },
    )
    logger.info("connected %s mailbox for user %s", verified.provider, user_id)


def get_access_token(user_id: int, provider: str = GOOGLE) -> str:
    """Return a usable access token, refreshing it when it has expired.

    Concurrency is the reason this is a function and not a SELECT. Three hosts
    run ingest, so several workers can want one user's token in the same
    second. The row is taken with SELECT ... FOR UPDATE for the whole
    read-decide-refresh-write sequence, and the decision is made from the row
    read *inside* that lock. That re-read is the point: a worker that queued
    behind a peer's refresh finds the peer's freshly stored token and returns
    it instead of performing a second refresh.

    Serialising buys more than a saved round trip. Google caps how many refresh
    tokens can be live per (client, user) and drops the oldest past the cap, so
    a burst of concurrent refreshes is a way to invalidate your own credential.
    And should the provider ever start returning a new refresh token on refresh
    - Google does not for this flow, but the write below handles it - unlocked
    racers would persist different successors, leaving the loser's stored and
    dead.

    Expiry is judged by Postgres's clock, in SQL, rather than by the calling
    host's. The three hosts do not share a clock; the database is the only one
    all of them agree on.
    """
    with db.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT refresh_token_enc, access_token_enc, invalid_at,
                   (access_token_enc IS NOT NULL
                    AND access_token_expires_at > now() + %s::interval) AS usable
            FROM user_oauth_tokens
            WHERE user_id = %s AND provider = %s
            FOR UPDATE
            """,
            (_EXPIRY_SKEW, user_id, provider),
        ).fetchone()
        if row is None:
            raise NotConnected(f"user {user_id} has no {provider} grant")
        if row["invalid_at"] is not None:
            raise NeedsReconnect(f"{provider} grant for user {user_id} was rejected")
        if row["usable"]:
            return crypto.decrypt(row["access_token_enc"])
        return _refresh_locked(conn, user_id, provider, crypto.decrypt(row["refresh_token_enc"]))


def _refresh_locked(conn: Connection, user_id: int, provider: str, refresh_token: str) -> str:
    """Refresh while holding the row lock taken by get_access_token."""
    resp = _post_token(
        {
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    if resp.status_code >= 400:
        error = _error_code(resp)
        if error == _DEAD_GRANT_ERROR:
            _mark_invalid(conn, user_id, provider, error)
            raise NeedsReconnect(f"{provider} grant for user {user_id} was rejected")
        raise ProviderError(f"token refresh failed: {error or resp.status_code}")

    payload = resp.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise ProviderError("token refresh returned no access token")
    conn.execute(
        """
        UPDATE user_oauth_tokens SET
            access_token_enc = %(access)s,
            access_token_expires_at = now() + make_interval(secs => %(expires_in)s),
            refresh_token_enc = COALESCE(%(refresh)s, refresh_token_enc),
            updated_at = now()
        WHERE user_id = %(user_id)s AND provider = %(provider)s
        """,
        {
            "access": crypto.encrypt(access_token),
            "expires_in": float(payload.get("expires_in") or 0),
            # Google does not rotate refresh tokens on this flow. Persisting one
            # when it does appear costs nothing and is the difference between a
            # silent expiry and none, should that ever change.
            "refresh": crypto.encrypt(payload["refresh_token"])
            if payload.get("refresh_token")
            else None,
            "user_id": user_id,
            "provider": provider,
        },
    )
    return access_token


def _mark_invalid(conn: Connection, user_id: int, provider: str, reason: str) -> None:
    """Record that the grant is dead, and commit it before the caller raises.

    The commit is load-bearing. This runs inside get_access_token's connection
    block, which rolls back when an exception leaves it - and an exception
    always does, because the whole point is to raise NeedsReconnect. Without an
    explicit commit the one durable trace of the failure would be discarded on
    the way out, and the system would go back to failing silently, which is the
    exact outcome this feature exists to prevent.
    """
    conn.execute(
        """
        UPDATE user_oauth_tokens SET
            invalid_at = now(), invalid_reason = %s,
            access_token_enc = NULL, access_token_expires_at = NULL, updated_at = now()
        WHERE user_id = %s AND provider = %s
        """,
        (reason, user_id, provider),
    )
    conn.commit()
    logger.warning(
        "%s grant for user %s rejected (%s); reconnect required", provider, user_id, reason
    )


def status(user_id: int, provider: str = GOOGLE) -> dict[str, Any]:
    """What the settings UI renders. `needs_reconnect` is derived from
    invalid_at rather than stored, so there is no second copy of the truth to
    drift from the column the refresh path actually writes."""
    row = db.query_one(
        "SELECT provider, account_email, scopes, invalid_at, invalid_reason, connected_at "
        "FROM user_oauth_tokens WHERE user_id = %s AND provider = %s",
        (user_id, provider),
    )
    if row is None:
        return {
            "connected": False,
            "provider": provider,
            "account_email": None,
            "scopes": [],
            "needs_reconnect": False,
            "invalid_reason": None,
            "connected_at": None,
        }
    return {
        "connected": True,
        "provider": row["provider"],
        "account_email": row["account_email"],
        "scopes": row["scopes"],
        "needs_reconnect": row["invalid_at"] is not None,
        "invalid_reason": row["invalid_reason"],
        "connected_at": row["connected_at"],
    }


def disconnect(user_id: int, provider: str = GOOGLE) -> bool:
    """Delete the stored grant, and tell the provider to drop it too.

    The local row goes first and unconditionally: a revoke call that fails must
    not leave a credential behind that the user has already been told is gone.
    Revoking is still attempted, because otherwise 'Disconnect' would leave a
    live grant sitting in the user's Google account.
    """
    row = db.query_one(
        "DELETE FROM user_oauth_tokens WHERE user_id = %s AND provider = %s "
        "RETURNING refresh_token_enc",
        (user_id, provider),
    )
    if row is None:
        return False
    try:
        requests.post(
            _REVOKE_ENDPOINT,
            data={"token": crypto.decrypt(row["refresh_token_enc"])},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning("provider revoke failed for user %s; row already deleted", user_id)
    return True
