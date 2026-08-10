from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.paths import PROJECT_ROOT

FILTERS_PATH = PROJECT_ROOT / "filters.toml"


@dataclass(frozen=True)
class FilterSpec:
    name: str
    prompt: str
    fail_closed: bool = False


def compute_prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_filter_specs() -> Dict[str, FilterSpec]:
    specs: Dict[str, FilterSpec] = {}
    default = os.environ.get("CUSTOM_FILTER_PROMPT", "").strip()
    if default:
        specs["default"] = FilterSpec("default", default)
    if FILTERS_PATH.exists():
        try:
            data = tomllib.loads(FILTERS_PATH.read_text())
        except Exception:
            data = {}
        for name, body in data.items():
            if not isinstance(body, dict):
                continue
            prompt = body.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                specs[name] = FilterSpec(name, prompt.strip(), bool(body.get("fail_closed", False)))
    return specs


def load_filters() -> Dict[str, str]:
    return {name: spec.prompt for name, spec in load_filter_specs().items()}


def get_filter_spec(name: str) -> Optional[FilterSpec]:
    return load_filter_specs().get(name)


def get_filter_prompt(name: str) -> Optional[str]:
    spec = get_filter_spec(name)
    return spec.prompt if spec else None


def list_filter_names() -> List[str]:
    return sorted(load_filter_specs().keys())
