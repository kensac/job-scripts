"""Reject-only shortcuts for explicitly opted-in filter revisions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.job_profile import JobProfileAnswer
from core.screening import _TECHNICAL

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


class VolumeGate(BaseModel):
    """Postings verification does not read for the listed boards and filters.

    A (source, title) pair judged `title_min_judged` times in `window_days`
    with no keep by any board or filter is skipped, except `audit_percent` of
    its postings (a fixed hash of the url), so a title that starts producing
    keeps returns on its own. 50 is the smallest cutoff that dropped no keep on
    three held-out splits (2026-09-17, 09-24, 10-01; 20 dropped 2 to 9, 10
    dropped 3 to 28). Titles the `occupation_words_v1` screen skips
    (core.screening) are skipped too. `scopes` names the exact prompt hashes that
    opt in; a target not listed reads everything as before.

    A source (a company's board) with at least `source_min_judged` postings
    judged in the window and a keep rate at or below `source_max_keep_rate` is
    skipped too, with the same audit sample. This is a volume decision, not a
    zero-loss one (Kanishk, 2026-10-09: "if there are companies that aren't
    getting on boards I don't see merit in keeping them"): on held-out splits a
    zero-keep source occasionally produced a later keep (Bank of America, RR
    Donnelley), which the audit sample is what brings back. Measured over the 30
    days to 2026-10-08: 50 judged and zero keeps covered 164 sources and 22% of
    the last week's judged volume, with no keep from them in those 30 days;
    a 0.2% keep rate would cover 35% and 68 of 18,566 keeps.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scopes: list[str] = Field(default_factory=list)
    window_days: int = Field(default=90, ge=7, le=365)
    audit_percent: int = Field(default=5, ge=0, le=100)
    occupation_titles: bool = True
    title_min_judged: int = Field(default=50, ge=0)
    source_min_judged: int = Field(default=50, ge=0)
    source_max_keep_rate: float = Field(default=0.0, ge=0.0, le=0.05)
