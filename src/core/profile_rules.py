"""Explicit taxonomy constraints, independent of a filter's prose instructions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from core.job_profile import (
    CareerStage,
    EmploymentType,
    JobProfileAnswer,
    RoleFamily,
    RoleTrack,
)


class ProfileRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    allowed_role_families: tuple[RoleFamily, ...] | None = None
    allowed_role_tracks: tuple[RoleTrack, ...] | None = None
    allowed_career_stages: tuple[CareerStage, ...] | None = None
    allowed_employment_types: tuple[EmploymentType, ...] | None = None
    people_manager: bool | None = None

    @field_validator(
        "allowed_role_families",
        "allowed_role_tracks",
        "allowed_career_stages",
        "allowed_employment_types",
        mode="before",
    )
    @classmethod
    def immutable_selection(cls, value: object) -> object:
        # Config arrives as JSON arrays; tuples prevent mutation after validation.
        return tuple(value) if isinstance(value, list) else value

    @field_validator(
        "allowed_role_families",
        "allowed_role_tracks",
        "allowed_career_stages",
        "allowed_employment_types",
    )
    @classmethod
    def meaningful_selection(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None:
            if not value:
                raise ValueError("omit a constraint instead of supplying an empty selection")
            if "unknown" in value:
                raise ValueError("unknown is missing evidence, not an allowed category")
            if len(set(value)) != len(value):
                raise ValueError("allowed categories must be unique")
        return value


@dataclass(frozen=True)
class ProfileRuleDecision:
    outcome: Literal["accept", "reject", "abstain"]
    reason: str


def evaluate(profile: JobProfileAnswer | None, rules: ProfileRules) -> ProfileRuleDecision:
    constraints = (
        ("primary_role_family", rules.allowed_role_families),
        ("career_stage", rules.allowed_career_stages),
        ("employment_type", rules.allowed_employment_types),
    )
    if (
        all(allowed is None for _, allowed in constraints)
        and rules.allowed_role_tracks is None
        and rules.people_manager is None
    ):
        return ProfileRuleDecision("abstain", "no_rules")
    if profile is None:
        return ProfileRuleDecision("abstain", "profile_missing")

    unknown: list[str] = []
    for field, allowed in constraints:
        if allowed is None:
            continue
        value = getattr(profile, field)
        if value == "unknown":
            unknown.append(field)
        elif value not in allowed:
            return ProfileRuleDecision("reject", f"{field}_mismatch")

    if rules.allowed_role_tracks is not None:
        tracks = profile.role_tracks
        if not any(track in rules.allowed_role_tracks for track in tracks):
            # A partial classification may conceal a matching second track.
            if not tracks or "unknown" in tracks:
                unknown.append("role_tracks")
            else:
                return ProfileRuleDecision("reject", "role_tracks_mismatch")

    if rules.people_manager is not None:
        if profile.people_manager is None:
            unknown.append("people_manager")
        elif profile.people_manager != rules.people_manager:
            return ProfileRuleDecision("reject", "people_manager_mismatch")

    if unknown:
        return ProfileRuleDecision("abstain", f"unknown:{','.join(unknown)}")
    return ProfileRuleDecision("accept", "configured_rules_satisfied")
