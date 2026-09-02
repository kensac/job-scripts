from __future__ import annotations

import hashlib
import logging
import os
import tomllib
from dataclasses import dataclass

from core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

FILTERS_PATH = PROJECT_ROOT / "filters.toml"


ON_AMBIGUOUS_VALUES = ("keep", "filter")

AMBIGUITY_RULES = {
    "keep": (
        "should_filter=true only if the job clearly violates the criteria; false if it matches "
        "or is ambiguous (prefer false negatives, do not lose good roles)."
    ),
    "filter": (
        "should_filter=true if the job violates the criteria, OR if the posting does not give you "
        "enough information to confirm it meets them; false only when it clearly meets them. "
        "For this filter ambiguity counts as a violation -- but this never means applying a harsher "
        "threshold than the criteria state, only that unconfirmed criteria fail."
    ),
}


def build_custom_instructions(prompt: str, on_ambiguous: str = "keep") -> str:
    if not prompt:
        return ""
    return f"""Evaluate a job against the user criteria below and decide whether to filter it out.

<user_criteria>
{prompt}
</user_criteria>

{AMBIGUITY_RULES.get(on_ambiguous, AMBIGUITY_RULES["keep"])}

reason: <=25 words citing the deciding factor (company/role/skills)."""


@dataclass(frozen=True)
class FilterSpec:
    name: str
    prompt: str
    fail_closed: bool = False
    on_ambiguous: str = "keep"


def compute_prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_filter_specs() -> dict[str, FilterSpec]:
    specs: dict[str, FilterSpec] = {}
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
                on_ambiguous = str(body.get("on_ambiguous", "keep")).lower()
                if on_ambiguous not in ON_AMBIGUOUS_VALUES:
                    logger.warning(
                        f"filter '{name}': invalid on_ambiguous={on_ambiguous!r}, "
                        f"expected one of {ON_AMBIGUOUS_VALUES}; defaulting to 'keep'"
                    )
                    on_ambiguous = "keep"
                specs[name] = FilterSpec(
                    name, prompt.strip(), bool(body.get("fail_closed", False)), on_ambiguous
                )
    return specs


def load_filters() -> dict[str, str]:
    return {name: spec.prompt for name, spec in load_filter_specs().items()}


def get_filter_spec(name: str) -> FilterSpec | None:
    return load_filter_specs().get(name)


def get_filter_prompt(name: str) -> str | None:
    spec = get_filter_spec(name)
    return spec.prompt if spec else None


def list_filter_names() -> list[str]:
    return sorted(load_filter_specs().keys())
