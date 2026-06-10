from __future__ import annotations

import hashlib
import os
import tomllib
from typing import Dict, List, Optional

from core.paths import PROJECT_ROOT

FILTERS_PATH = PROJECT_ROOT / "filters.toml"


def compute_prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_filters() -> Dict[str, str]:
    filters: Dict[str, str] = {}
    default = os.environ.get("CUSTOM_FILTER_PROMPT", "").strip()
    if default:
        filters["default"] = default
    if FILTERS_PATH.exists():
        try:
            data = tomllib.loads(FILTERS_PATH.read_text())
        except Exception:
            data = {}
        for name, body in data.items():
            prompt = body.get("prompt") if isinstance(body, dict) else None
            if isinstance(prompt, str) and prompt.strip():
                filters[name] = prompt.strip()
    return filters


def get_filter_prompt(name: str) -> Optional[str]:
    return load_filters().get(name)


def list_filter_names() -> List[str]:
    return sorted(load_filters().keys())
