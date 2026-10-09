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


# Occupations no board or filter has kept, as whole title words. Derived
# 2026-10-07 from every custom verdict since 2026-08-01 (all prompts): a word in
# at least 300 rejected non-technical titles across at least 10 sources and in
# no kept non-technical title. Built from verdicts before 2026-10-01, it dropped
# 0 of 1,129 keeps after; the 47 below are that list less its street names,
# brands and generic words, and drop 0 of 27,241 keeps over all history. The
# technical words above still win, so "Nurse Informatics Engineer" is reviewed.
OCCUPATION_WORDS = (
    "accountant", "accounting", "aide", "auditor", "baker", "bakery", "behavioral",
    "beverage", "billing", "cardiology", "cdl", "chef", "cleaner", "clinician", "cook",
    "diesel", "expeditor", "forklift", "icu", "infusion", "inpatient", "janitorial",
    "kitchen", "loader", "lpn", "neurology", "nurse", "pathologist", "patient", "payroll",
    "pediatrics", "pharmacist", "physician", "picker", "porter", "practitioner",
    "recruiting", "respiratory", "secretary", "sous", "speech", "teacher", "teller",
    "therapist", "therapy", "underwriter", "urology",
)  # fmt: skip


def _postgres(pattern: str) -> str:
    # Python spells a word boundary \b; PostgreSQL's ARE engine spells it \y.
    return pattern.replace(r"\b", r"\y")


TECHNICAL_SQL_PATTERN = _postgres(_TECHNICAL.pattern)
OCCUPATION_SQL_PATTERN = _postgres(r"\b(?:" + "|".join(OCCUPATION_WORDS) + r")\b")


class VolumeGate(BaseModel):
    """Postings verification does not read for the listed boards and filters.

    A source is skipped once at least `min_judged` of its postings have been
    judged in `window_days` and none was kept by any board or filter. Zero keeps
    in 300 puts the source's keep rate under 1% with 95% confidence (3/300), no
    better than an average posting, which boards keep about 1% of the time.
    `audit_percent` of its postings (a fixed hash of the url) are still read, so
    a source that starts producing keeps leaves the list on its own. Titles
    naming an occupation in OCCUPATION_WORDS, with no technical word, are
    skipped too. `scopes` names the exact prompt hashes that opt in; a target
    not listed reads everything as before.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scopes: list[str] = Field(default_factory=list)
    min_judged: int = Field(default=300, ge=50)
    window_days: int = Field(default=90, ge=7, le=365)
    audit_percent: int = Field(default=5, ge=0, le=100)
    occupation_titles: bool = True
    # A (source, title) pair judged this many times with no keep is skipped
    # like an unproductive source, sharing its audit sample; 0 turns it off.
    # 50 is the smallest that dropped no keep on three held-out splits
    # (2026-09-17, 09-24, 10-01; 20 dropped 2 to 9, 10 dropped 3 to 28), and it
    # skipped 19.7% of what verification still read after #821 (2026-10-08).
    title_min_judged: int = Field(default=50, ge=0)
