"""Provider configuration and typed failures shared by OAuth flows."""

from __future__ import annotations

import os

from api.config import ALL_GROUPS as ALL_GROUPS
from api.config import group_access_allowed
from core.env import env_list

GOOGLE = "google"

# Read-only. Wider Gmail scopes can send, modify, or delete mail. The scope is
# asserted by the test suite so it cannot be widened quietly.
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
# Google compares registered redirect URIs byte for byte. Preview deployment
# hostnames therefore cannot be registered through this static list.
REGISTERED_REDIRECT_URIS = (
    "https://www.kanishksachdev.com/job-tracker/settings/gmail/callback",
    "http://localhost:3000/job-tracker/settings/gmail/callback",
)
# Covers consent while remaining within Google's authorization-code lifetime.
STATE_TTL_SECONDS = 600
# The database clock decides expiry. This margin covers an entire ingest run.
EXPIRY_SKEW = "2 minutes"
# Refresh runs while holding a Postgres row lock. This bounds lock duration
# when the token endpoint stalls.
HTTP_TIMEOUT = 10
# Only invalid_grant means the person must reconnect. Other endpoint failures
# are provider or operator failures and must not become reconnect prompts.
DEAD_GRANT_ERROR = "invalid_grant"
# Persisted app_config key, editable through the admin config route.
CONNECT_GROUPS_KEY = "gmail_connect_groups"


class OAuthError(Exception):
    """Base for failures a route needs to distinguish."""


class NotConnected(OAuthError):
    """No stored grant for this user and provider."""


class NeedsReconnect(OAuthError):
    """The provider rejected the stored refresh token."""


class StateInvalid(OAuthError):
    """The state was forged, expired, or issued to another user."""


class ScopeDeclined(OAuthError):
    """The user completed consent without the required scope."""


class ProviderError(OAuthError):
    """The provider endpoint or worker configuration failed."""


def required_env(name: str) -> str:
    """Distinguish a worker missing credentials from a dead user grant."""
    try:
        return os.environ[name]
    except KeyError:
        raise ProviderError(
            f"{name} is not set on worker {os.environ.get('JOBTRACKER_WORKER_NAME', '?')}; "
            "the grant is fine, this host cannot refresh it"
        ) from None


def client_id() -> str:
    return required_env("GOOGLE_OAUTH_CLIENT_ID")


def client_secret() -> str:
    # Read at call time because the credential is injected after process start.
    return required_env("GOOGLE_OAUTH_CLIENT_SECRET")


def redirect_uris() -> list[str]:
    return env_list("GMAIL_OAUTH_REDIRECT_URIS") or list(REGISTERED_REDIRECT_URIS)


def connect_allowed(groups: list[str]) -> bool:
    return group_access_allowed(CONNECT_GROUPS_KEY, groups)
