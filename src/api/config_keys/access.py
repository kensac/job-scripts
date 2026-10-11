"""Persisted access settings and their admin metadata."""

from __future__ import annotations

from api.config_types import ConfigKey

CONFIG_KEYS: dict[str, ConfigKey] = {
    "signups_enabled": ConfigKey(
        section="Access",
        default=True,
        value_type=bool,
        help="Whether brand-new people can create tracker accounts. Existing users, and the internal and admin groups, get in either way.",
    ),
    # Testing-mode OAuth is limited to 100 test users; keep the default
    # closed to other groups until the client is ready for them.
    "gmail_connect_groups": ConfigKey(
        section="Access",
        default=["infra-admins"],
        value_type=list[str],
        kind="groups",
        help="Authentik groups whose members may connect a mailbox.",
    ),
}
