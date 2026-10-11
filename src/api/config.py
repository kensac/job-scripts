"""Persisted application setting registry and group access policy."""

from __future__ import annotations

import logging

from api.config_keys import (
    access,
    applications,
    boards,
    catalog,
    extension,
    fetching,
    fleet,
    health,
    mail,
)
from api.config_types import ColumnState as ColumnState
from api.config_types import ConfigKey as ConfigKey
from api.config_types import SortKey as SortKey

logger = logging.getLogger(__name__)
ALL_GROUPS = "*"

_CONFIG_KEYS_BY_SECTION: dict[str, dict[str, ConfigKey]] = {
    "Boards": boards.CONFIG_KEYS,
    "Extension": extension.CONFIG_KEYS,
    "Access": access.CONFIG_KEYS,
    "Fetching": fetching.CONFIG_KEYS,
    "Health": health.CONFIG_KEYS,
    "Catalog": catalog.CONFIG_KEYS,
    "Applications": applications.CONFIG_KEYS,
    "Mail": mail.CONFIG_KEYS,
    "Fleet": fleet.CONFIG_KEYS,
}

CONFIG_KEYS: dict[str, ConfigKey] = {
    key: spec
    for section_keys in _CONFIG_KEYS_BY_SECTION.values()
    for key, spec in section_keys.items()
}


def group_access_allowed(key: str, groups: list[str]) -> bool:
    from api import db

    spec = CONFIG_KEYS[key]
    if spec.kind != "groups":
        raise ValueError(f"{key} is not a group policy")
    try:
        allowed = spec.validate(db.get_config(key))
    except ValueError:
        logger.warning("%s has invalid group configuration, refusing access", key)
        return False
    return isinstance(allowed, list) and (
        ALL_GROUPS in allowed or any(group in allowed for group in groups)
    )
