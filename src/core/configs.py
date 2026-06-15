from __future__ import annotations

import tomllib
from typing import Dict, List

from core.paths import PROJECT_ROOT

CONFIGS_PATH = PROJECT_ROOT / "configs.toml"


def _load() -> dict:
    if not CONFIGS_PATH.exists():
        return {}
    try:
        return tomllib.loads(CONFIGS_PATH.read_text())
    except Exception:
        return {}


def load_configs() -> Dict[str, Dict[str, str]]:
    configs: Dict[str, Dict[str, str]] = {}
    for name, body in (_load().get("configs") or {}).items():
        if not isinstance(body, dict):
            continue
        sheet_id = body.get("sheet_id")
        job_listings_url = body.get("job_listings_url")
        if isinstance(sheet_id, str) and isinstance(job_listings_url, str):
            configs[name] = {"SHEET_ID": sheet_id, "JOB_LISTINGS_URL": job_listings_url}
    return configs


def load_groups() -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for name, members in (_load().get("groups") or {}).items():
        if isinstance(members, list) and all(isinstance(m, str) for m in members):
            groups[name] = members
    return groups
