"""Shared prompt-version semantics and response models for filter insights.

The unit here is a PROMPT VERSION, not a filter. `ai_queries` carries no
filter id - only `prompt_hash` and a free-text `filter_name` - and neither is a
stable identity: `pay_tier_200` has been two different prompts, and one prompt
has been called "default", "general" and "user1:default". Keying a row on
filter identity would therefore average two different filters together across
a prompt edit and look perfectly fine while doing it. Keying it on prompt_hash
makes that impossible by construction, and `sibling_hashes_by_name` tells a
caller, per name, when the name it is showing spans more than one version.

Ownership is only partly knowable and the payload says so rather than
guessing. `user_filters.prompt_hash` holds the CURRENT hash only, so editing a
prompt orphans every verdict the old one produced. Measured over the corpus,
52% of rejections resolve to exactly one owner, 19% to several users holding
identical filter text, and 29% to nobody at all - edited or deleted, with
nothing in the schema to say which.
"""

from __future__ import annotations

import collections
import datetime
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from core.reason_taxonomy import (
    EVIDENCE_MISSING_DESCRIPTION,
    EVIDENCE_MISSING_PHRASES,
    GROUP_LABELS,
    GROUPS,
    classify,
    is_evidence_missing,
)

# A share is only worth rendering if one decision cannot swing it by more than
# a couple of points; at two points that is 1/0.02 decisions. Callers get the
# threshold back in the response so the UI and the API draw the same line
# rather than each inventing one.
_MAX_SWING_PER_DECISION = 0.02
DEFAULT_MIN_DECISIONS = int(1 / _MAX_SWING_PER_DECISION)

DECISION_WINDOW_SQL = "created_at >= now() - make_interval(days => %(days)s)"

# The residual bucket, named once. It is both the key in the response and the
# value `group` accepts on the phrasings listing, so a caller can drill into
# "matched nothing" the same way it drills into any named group. Cannot
# collide with a taxonomy key - those are all specific.
UNGROUPED_REASON_KEY = "ungrouped"


class EvidenceCriterion(BaseModel):
    """What "evidence missing" means, in the payload rather than in whoever
    explained it once."""

    method: str
    description: str
    phrases: list[str]


class ExamplesSelection(BaseModel):
    """How the examples were chosen, for the same reason."""

    method: str
    ordered_by: str
    description: str


class ReasonGroup(BaseModel):
    key: str
    label: str


class Taxonomy(BaseModel):
    groups: list[ReasonGroup]
    evidence_missing_criterion: EvidenceCriterion


class Phrasing(BaseModel):
    """One distinct rejection sentence, with how often it was written."""

    phrasing: str
    decisions: int
    distinct_jobs: int


class PhrasingPage(BaseModel):
    """`filters` echoes what narrowed this page, keyed by parameter name, and
    carries only the parameters that narrowed anything."""

    prompt_hash: str
    group: str | None
    window_days: int
    total_phrasings: int
    total_decisions: int
    offset: int
    limit: int
    returned: int
    has_more: bool
    phrasings: list[Phrasing]
    filters: dict[str, list[str]]
    filterable: list[str]


class Breakdown(BaseModel):
    """One bucket of rejections. `examples` is a reproducible sample of
    `distinct_phrasings`, not a frequency ranking; see `_finish`."""

    decisions: int
    distinct_jobs: int
    distinct_phrasings: int
    evidence_missing_decisions: int
    evidence_missing_distinct_jobs: int
    examples: list[str]


class GroupBreakdown(Breakdown):
    """A bucket that a taxonomy group named. The residual bucket is the same
    shape without the name, and rides on the row as `ungrouped`."""

    key: str
    label: str


class PromptTotals(BaseModel):
    """`rejected_with_reason` is the denominator the groups are of, which is
    not `rejected`: the batched paths recorded no reason for a stretch."""

    evaluated: int
    passed: int
    rejected: int
    rejected_with_reason: int
    distinct_jobs_evaluated: int
    distinct_jobs_rejected: int
    evidence_missing_decisions: int
    evidence_missing_distinct_jobs: int


class OwnedFilter(BaseModel):
    """A filter that carries this prompt today."""

    name: str
    enabled: bool


class OwnerUser(BaseModel):
    sub: str
    email: str


class PromptOwner(BaseModel):
    """Who holds this prompt, and whether it can still reject anything.

    `state` is resolved, shared or unknown: a prompt that was edited or
    deleted resolves to nobody, and 29% of rejections measured over the
    corpus are in that state. `enabled` is null when no current filter
    carries this prompt at all, because then there is nothing to ask.
    """

    state: str
    user_count: int
    users: list[OwnerUser]
    enabled: bool | None
    filters: list[OwnedFilter]


class PromptVersion(BaseModel):
    """One prompt version, which is the unit here rather than one filter.

    `sibling_hashes_by_name` is per name, not a single number: a hash can
    carry several names, and a scalar would leave a caller unable to say
    which name it describes.
    """

    prompt_hash: str
    filter_names: list[str]
    sibling_hashes_by_name: dict[str, int]
    first_seen: datetime.datetime | None
    last_seen: datetime.datetime | None
    owner: PromptOwner
    sufficient: bool
    totals: PromptTotals
    groups: list[GroupBreakdown]
    ungrouped: Breakdown


class MyPromptVersion(BaseModel):
    """The same prompt version as its owner sees it: their own filter names
    rather than every name the prompt has been given, and no owner block,
    because they are it."""

    prompt_hash: str
    filters: list[OwnedFilter]
    first_seen: datetime.datetime | None
    last_seen: datetime.datetime | None
    sufficient: bool
    totals: PromptTotals
    groups: list[GroupBreakdown]
    ungrouped: Breakdown


class ReasonsEnvelope(BaseModel):
    """What both rejection-reason views say about how they counted, so the
    two surfaces cannot describe the same prompt differently."""

    window_days: int
    unit: str = "decisions"
    min_decisions: int
    overlapping_groups: bool = True
    examples_selection: ExamplesSelection
    evidence_missing_criterion: EvidenceCriterion


class RejectionReasons(ReasonsEnvelope):
    prompt_versions: list[PromptVersion]
    filters: dict[str, list[str]]
    filterable: list[str]


class MyRejectionReasons(ReasonsEnvelope):
    prompt_versions: list[MyPromptVersion]


def _evidence_criterion() -> EvidenceCriterion:
    return EvidenceCriterion(
        method="phrase_match",
        description=EVIDENCE_MISSING_DESCRIPTION,
        phrases=list(EVIDENCE_MISSING_PHRASES),
    )


def _example_selection() -> ExamplesSelection:
    """How the examples were chosen, published for the same reason the
    evidence-missing criterion is: a caller that has to narrate a number will
    narrate it from somewhere, and if the derivation is not in the payload it
    comes from memory, or from whoever explained it once. That is not
    hypothetical - `distinct_phrasings` counts exactly what its name says, and
    a page still rendered "the three most common" over it, because the field
    was named correctly and the sentence around it was not.
    """
    return ExamplesSelection(
        method="sample",
        ordered_by="decisions desc, then phrasing",
        description=(
            "A reproducible sample of `distinct_phrasings`, not a frequency "
            "ranking. Phrasings in this corpus almost never repeat, so counts "
            "are nearly all 1 and there is no meaningful 'most common'."
        ),
    )


@dataclass(slots=True)
class ReasonBreakdownAccumulator:
    """Count decisions in one reason group while retaining distinct URLs."""

    decisions: int = 0
    _jobs: set[str] = field(default_factory=set)
    _phrasings: collections.Counter[str] = field(default_factory=collections.Counter)
    _evidence_missing_decisions: int = 0
    _evidence_missing_jobs: set[str] = field(default_factory=set)

    def record(self, reason: str, url: str, *, evidence_missing: bool) -> None:
        self.decisions += 1
        self._jobs.add(url)
        self._phrasings[reason] += 1
        if evidence_missing:
            self._evidence_missing_decisions += 1
            self._evidence_missing_jobs.add(url)

    def to_breakdown(self, examples: int) -> Breakdown:
        return Breakdown(
            decisions=self.decisions,
            distinct_jobs=len(self._jobs),
            distinct_phrasings=len(self._phrasings),
            evidence_missing_decisions=self._evidence_missing_decisions,
            evidence_missing_distinct_jobs=len(self._evidence_missing_jobs),
            # Counts rank the examples, then text breaks ties reproducibly.
            # This is a sample of distinct phrasings, not a representative
            # frequency ranking when most phrasings occur once.
            examples=[
                text
                for text, _ in sorted(self._phrasings.items(), key=lambda kv: (-kv[1], kv[0]))[
                    :examples
                ]
            ],
        )


@dataclass(slots=True)
class PromptEvidenceAccumulator:
    """Count missing-evidence decisions once, even across overlapping groups."""

    decisions: int = 0
    _jobs: set[str] = field(default_factory=set)

    def record(self, url: str) -> None:
        self.decisions += 1
        self._jobs.add(url)

    @property
    def distinct_job_count(self) -> int:
        return len(self._jobs)


def _group_rejections_by_prompt(
    hashes: list[str], rejections: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, ReasonBreakdownAccumulator]], dict[str, PromptEvidenceAccumulator]]:
    """One classification pass, shared by the admin and user views.

    A reason can land in several groups, so the per-hash evidence-missing
    totals ride along here rather than being summed from the groups
    afterwards: a reason in three groups is still one decision.
    """
    grouped: dict[str, dict[str, ReasonBreakdownAccumulator]] = {
        h: collections.defaultdict(ReasonBreakdownAccumulator) for h in hashes
    }
    per_hash = {h: PromptEvidenceAccumulator() for h in hashes}
    for row in rejections:
        buckets = grouped[row["prompt_hash"]]
        reason, url = row["reason"], row["url"]
        missing = is_evidence_missing(reason)
        if missing:
            per_hash[row["prompt_hash"]].record(url)
        keys = classify(reason) or (UNGROUPED_REASON_KEY,)
        for key in keys:
            buckets[key].record(reason, url, evidence_missing=missing)
    return grouped, per_hash


def _prompt_totals(row: dict[str, Any], missing: PromptEvidenceAccumulator) -> PromptTotals:
    return PromptTotals(
        evaluated=row["evaluated"],
        passed=row["passed"],
        rejected=row["rejected"],
        rejected_with_reason=row["rejected_with_reason"],
        distinct_jobs_evaluated=row["distinct_jobs_evaluated"],
        distinct_jobs_rejected=row["distinct_jobs_rejected"],
        evidence_missing_decisions=missing.decisions,
        evidence_missing_distinct_jobs=missing.distinct_job_count,
    )


def _named_group_breakdowns(
    buckets: dict[str, ReasonBreakdownAccumulator], examples: int
) -> list[GroupBreakdown]:
    return sorted(
        (
            GroupBreakdown(
                **buckets[g.key].to_breakdown(examples).model_dump(),
                key=g.key,
                label=GROUP_LABELS[g.key],
            )
            for g in GROUPS
            if g.key in buckets
        ),
        key=lambda g: g.decisions,
        reverse=True,
    )
