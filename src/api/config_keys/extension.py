"""Persisted extension settings and their admin metadata."""

from __future__ import annotations

from api.apply.policy import ExtensionPolicy
from api.config_types import ConfigKey

CONFIG_KEYS: dict[str, ConfigKey] = {
    "extension_policy": ConfigKey(
        section="Extension",
        default=ExtensionPolicy().model_dump(mode="json"),
        value_type=ExtensionPolicy,
        help="Controls bundled extension features and disables adapters by reader ID. "
        "One atomic policy; no scripts, selectors or navigation commands. "
        "Requires an extension supporting configuration schema 1; older releases ignore it.",
    ),
}
