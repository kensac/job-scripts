"""Persisted access settings and their admin metadata."""

from __future__ import annotations

from pydantic import NonNegativeInt

from api.admin_groups import ADMIN_GROUPS
from api.config_types import ConfigKey

CONFIG_KEYS: dict[str, ConfigKey] = {
    "signups_enabled": ConfigKey(
        section="Access",
        default=True,
        value_type=bool,
        help="Whether brand-new people can create tracker accounts. Existing users, and the internal and admin groups, get in either way.",
    ),
    # Testing-mode OAuth is limited to 100 test users; keep the default
    # closed to all but the admin groups (JOBTRACKER_ADMIN_GROUPS) until the
    # client is ready for them.
    "gmail_connect_groups": ConfigKey(
        section="Access",
        default=[group for group in sorted(ADMIN_GROUPS)],
        value_type=list[str],
        kind="groups",
        help="Authentik groups whose members may connect a mailbox.",
    ),
    # A judgment, not a measurement: nothing anonymous was accepted before
    # this key, so there is no volume to derive it from. It bounds what a
    # script can put in the inbox in an hour, across every list. Revisit it
    # against the inbox's real hourly counts once there are some.
    "public_feedback_hourly_limit": ConfigKey(
        section="Access",
        default=60,
        value_type=NonNegativeInt,
        help="How many anonymous notes and posting reports the public job lists accept in any "
        "hour, across every list together. Past it, visitors are told to try again later and "
        "nothing is stored. Zero turns the forms off.",
    ),
}
