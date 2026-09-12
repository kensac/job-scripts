"""Cheap, versioned title gates for managed-board model calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

TitleGateRecipe = Literal["internship_v1", "new_grad_v1"]
TitleGateMode = Literal["shadow", "enforce"]


class TitleGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe: TitleGateRecipe
    mode: TitleGateMode = "shadow"


@dataclass(frozen=True)
class TitleGateDecision:
    keep: bool
    reason: str


_INTERNSHIP_SIGNAL = re.compile(
    r"\b(?:intern(?:ship)?s?|co[ -]?op|student|university|summer (?:analyst|associate)|intensive program)\b",
    re.IGNORECASE,
)
_EXPLICIT_INTERNSHIP = re.compile(
    r"\b(?:intern(?:ship)?s?|co[ -]?op|summer (?:analyst|associate)|intensive program)\b",
    re.IGNORECASE,
)
_EXPERIENCED = re.compile(
    r"\b(?:senior|sr\.?|principal|lead|director|head|vice president|vp|chief)\b",
    re.IGNORECASE,
)
_CURATED_INTERNSHIP_SOURCES = frozenset(
    {"internships", "speedyapply_intern", "speedyapply_ai_intern", "internships_ouckah"}
)


def evaluate(config: TitleGateConfig | None, *, title: str, source: str) -> TitleGateDecision:
    """Return whether a posting deserves the detailed managed-board pass."""
    if config is None:
        return TitleGateDecision(True, "disabled")
    if config.recipe == "internship_v1":
        if source in _CURATED_INTERNSHIP_SOURCES:
            return TitleGateDecision(True, "curated_internship_source")
        if _INTERNSHIP_SIGNAL.search(title):
            return TitleGateDecision(True, "internship_title_signal")
        return TitleGateDecision(False, "no_internship_title_signal")
    if _EXPLICIT_INTERNSHIP.search(title):
        return TitleGateDecision(False, "internship_title_signal")
    if _EXPERIENCED.search(title):
        return TitleGateDecision(False, "experienced_title_signal")
    return TitleGateDecision(True, "possible_new_grad_title")
