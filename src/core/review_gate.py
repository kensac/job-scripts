"""Reject-only shortcuts for explicitly opted-in filter revisions."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.job_profile import JobProfileAnswer

Mode = Literal["off", "shadow", "enforce"]


class ReviewGateScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # A prose edit changes its hash and deliberately removes the opt-in.
    title_recipe: Literal["nontechnical_occupations_v1"] | None = None
    profile_recipe: Literal["nontechnical_families_v1"] | None = None


class ReviewGatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title_mode: Mode = "off"
    profile_mode: Mode = "off"
    scopes: dict[str, ReviewGateScope] = Field(default_factory=dict)
    lookup_timeout_ms: int = Field(default=1000, gt=0, le=5000)


# A technical qualifier wins over an occupation match. Broad titles such as
# analyst, associate, manager, technician and operations never reject alone.
_TECHNICAL = re.compile(
    r"\b(?:engineer\w*|software|developer\w*|data|analyst\w*|product|program|"
    r"technical|technology|computer|computing|systems?|infrastructure|platform|"
    r"automation|robot\w*|firmware|embedded|machine learning|artificial intelligence|"
    r"research|scientist|science|cyber\w*|devops|sre|it|ux|ui|solutions?|"
    r"consult\w*|quant\w*|ai|ml)\b",
    re.I,
)
_OCCUPATIONS = {
    "clinical_care": re.compile(
        r"\b(?:registered nurse|licensed (?:practical|vocational) nurse|nursing assistant|"
        r"nurse practitioner|phlebotomist|pharmacist|dental hygienist|"
        r"physical therapist|occupational therapist|radiologic technologist)\b",
        re.I,
    ),
    "retail_service": re.compile(
        r"\b(?:cashier|retail sales associate|store manager|customer service rep(?:resentative)?|"
        r"deli clerk|night crew stocker|in-store shopper)\b",
        re.I,
    ),
    "driving": re.compile(r"\b(?:delivery driver|commercial driver|truck driver)\b", re.I),
    "hospitality_cleaning": re.compile(
        r"\b(?:janitorial cleaner|custodial cleaner|custodian|housekeeper|dishwasher|"
        r"bartender|line cook|prep cook|restaurant server)\b",
        re.I,
    ),
}


def title_rejection(title: str) -> str | None:
    if not title.strip() or _TECHNICAL.search(title):
        return None
    for reason, pattern in _OCCUPATIONS.items():
        if pattern.search(title):
            return reason
    return None


def profile_rejection(profile: JobProfileAnswer, title: str = "") -> str | None:
    # No inferred seniority, experience, prestige or compensation shortcuts.
    # Any technical/unknown track may conceal relevant adjacent responsibilities.
    if not title.strip() or _TECHNICAL.search(title):
        return None
    if profile.primary_role_family not in {"sales", "marketing", "finance", "legal", "people"}:
        return None
    if profile.role_tracks != ["other"]:
        return None
    return "nontechnical_family:" + profile.primary_role_family
