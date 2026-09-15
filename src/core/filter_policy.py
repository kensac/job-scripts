"""Revision-bound proposals, kept separate from paid filter verdicts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.job_profile import JobProfileAnswer
from core.profile_rules import ProfileRules, evaluate
from core.title_screen import TitleScreenArtifact
from core.title_screen import evaluate as screen_title


class ProfilePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: ProfileRules
    # Matching a subset cannot establish that an arbitrary prose filter passes.
    covers_entire_filter: bool = False


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_mode: Literal["off", "shadow"] = "off"
    title_mode: Literal["off", "shadow"] = "off"
    ambiguity_mode: Literal["off", "shadow"] = "off"
    # The observer is optional: bound its database overhead independently of review.
    observation_timeout_ms: int = Field(default=1000, gt=0)
    profiles: dict[str, ProfilePolicy] = Field(default_factory=dict)
    titles: dict[str, TitleScreenArtifact] = Field(default_factory=dict)


class RouteProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["filter-routing-v1"] = "filter-routing-v1"
    outcome: Literal["accept", "reject", "abstain"]
    stage: Literal["profile", "title", "detailed"]
    reason: str
    would_review: bool
    profile_id: int | None = None
    title_artifact: str | None = None
    profile_policy: ProfilePolicy | None = None
    title_reason: str | None = None
    title_keep_probability_upper_bound: float | None = None


def propose(
    policy: RoutingPolicy,
    prompt_hash: str,
    profile: JobProfileAnswer | None,
    *,
    title: str = "",
    model: str | None = None,
    profile_id: int | None = None,
) -> RouteProposal:
    outcome: Literal["accept", "reject", "abstain"] = "abstain"
    stage: Literal["profile", "title", "detailed"] = "detailed"
    reason = "no_applicable_policy"
    artifact = policy.titles.get(prompt_hash) if policy.title_mode == "shadow" else None
    title_decision = screen_title(title, prompt_hash, artifact, model=model)
    if policy.title_mode == "shadow":
        reason = title_decision.reason
    if policy.title_mode == "shadow" and title_decision.outcome == "reject":
        outcome, stage, reason = "reject", "title", title_decision.reason
    elif policy.profile_mode == "shadow" and prompt_hash in policy.profiles:
        configured = policy.profiles[prompt_hash]
        decision = evaluate(profile, configured.rules)
        outcome, reason = decision.outcome, decision.reason
        if outcome == "accept" and not configured.covers_entire_filter:
            outcome, reason = "abstain", "uncovered_filter_requirements"
        if outcome != "abstain":
            stage = "profile"
    return RouteProposal(
        outcome=outcome,
        stage=stage,
        reason=reason,
        would_review=policy.ambiguity_mode == "off" or outcome == "abstain",
        profile_id=profile_id,
        title_artifact=artifact.fingerprint if artifact else None,
        profile_policy=policy.profiles.get(prompt_hash)
        if policy.profile_mode == "shadow"
        else None,
        title_reason=title_decision.reason if policy.title_mode == "shadow" else None,
        title_keep_probability_upper_bound=title_decision.keep_probability_upper_bound,
    )
