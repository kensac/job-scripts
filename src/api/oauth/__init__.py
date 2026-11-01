"""Per-user OAuth grant storage, refresh, and revocation.

Consent state and provider configuration have separate owners. This package keeps the public `api.oauth` import path for routes and workers.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from psycopg import Connection

from api import crypto, db
from api.oauth import authorization as consent
from api.oauth import provider

authorization_url = consent.authorization_url
issue_state = consent.issue_state
verify_state = consent.verify_state

ALL_GROUPS = provider.ALL_GROUPS
CONNECT_GROUPS_KEY = provider.CONNECT_GROUPS_KEY
GMAIL_SCOPES = provider.GMAIL_SCOPES
GOOGLE = provider.GOOGLE
STATE_TTL_SECONDS = provider.STATE_TTL_SECONDS
NotConnected = provider.NotConnected
NeedsReconnect = provider.NeedsReconnect
StateInvalid = provider.StateInvalid
ScopeDeclined = provider.ScopeDeclined
ProviderError = provider.ProviderError
_client_id = provider.client_id
_client_secret = provider.client_secret
connect_allowed = provider.connect_allowed
redirect_uris = provider.redirect_uris

_TOKEN_ENDPOINT = provider.TOKEN_ENDPOINT
_REVOKE_ENDPOINT = provider.REVOKE_ENDPOINT
_PROFILE_ENDPOINT = provider.PROFILE_ENDPOINT
_EXPIRY_SKEW = provider.EXPIRY_SKEW
_HTTP_TIMEOUT = provider.HTTP_TIMEOUT
_DEAD_GRANT_ERROR = provider.DEAD_GRANT_ERROR

logger = logging.getLogger(__name__)


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
    except requests.RequestException, ValueError:
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
