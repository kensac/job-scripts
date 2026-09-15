"""Reject-only exact-title evidence, scoped to one immutable filter prompt."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_title(title: str) -> str:
    # Do not erase seniority, punctuation, location or employment-type qualifiers.
    return " ".join(title.casefold().split())


class TitleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    job_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    passed: bool

    @field_validator("job_key", "title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence identity and title must not be blank")
        return value


@dataclass(frozen=True)
class _TitleCounts:
    training_count: int
    training_keeps: int
    held_out_count: int
    held_out_keeps: int


class TitleEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str = Field(min_length=1)
    training_count: int = Field(ge=0)
    training_keeps: int = Field(ge=0)
    held_out_count: int = Field(ge=0)
    held_out_keeps: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_counts(self) -> Self:
        if not normalize_title(self.title):
            raise ValueError("title must not be blank")
        if self.training_keeps > self.training_count or self.held_out_keeps > self.held_out_count:
            raise ValueError("keep counts cannot exceed population counts")
        return self


class TitleScreenArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["exact-title-v1"] = "exact-title-v1"
    prompt_hash: str = Field(min_length=1)
    reference_model: str = Field(min_length=1)
    evidence: tuple[TitleEvidence, ...]
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_keep_probability: float = Field(ge=0, lt=1)
    confidence: float = Field(gt=0, lt=1)

    @field_validator("evidence", mode="before")
    @classmethod
    def immutable_observations(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("prompt_hash", "reference_model")
    @classmethod
    def nonblank_hash(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt hash must not be blank")
        return value

    @model_validator(mode="after")
    def unique_titles(self) -> Self:
        titles = [normalize_title(item.title) for item in self.evidence]
        if len(titles) != len(set(titles)):
            raise ValueError("evidence must contain each normalized title only once")
        return self

    @cached_property
    def counts(self) -> Mapping[str, _TitleCounts]:
        return MappingProxyType(
            {
                normalize_title(item.title): _TitleCounts(
                    item.training_count,
                    item.training_keeps,
                    item.held_out_count,
                    item.held_out_keeps,
                )
                for item in self.evidence
            }
        )

    @cached_property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @cached_property
    def candidate_count(self) -> int:
        # Candidate selection uses training only; held-out labels cannot choose tests.
        return sum(
            counts.training_count > 0 and counts.training_keeps == 0
            for counts in self.counts.values()
        )


def build_artifact(
    *,
    prompt_hash: str,
    reference_model: str,
    training: tuple[TitleObservation, ...],
    held_out: tuple[TitleObservation, ...],
    max_keep_probability: float,
    confidence: float,
) -> TitleScreenArtifact:
    identities = [observation.job_key for observation in (*training, *held_out)]
    if len(identities) != len(set(identities)):
        raise ValueError("each posting may occur only once across both evidence partitions")
    by_title: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    digest = hashlib.sha256()
    for observations, offset in ((training, 0), (held_out, 2)):
        for observation in sorted(observations, key=lambda item: item.job_key):
            digest.update(f"{offset}:{observation.model_dump_json()}\n".encode())
            counts = by_title[normalize_title(observation.title)]
            counts[offset] += 1
            counts[offset + 1] += int(observation.passed)
    return TitleScreenArtifact(
        prompt_hash=prompt_hash,
        reference_model=reference_model,
        evidence=tuple(
            TitleEvidence(
                title=title,
                training_count=counts[0],
                training_keeps=counts[1],
                held_out_count=counts[2],
                held_out_keeps=counts[3],
            )
            for title, counts in sorted(by_title.items())
        ),
        evidence_digest=digest.hexdigest(),
        max_keep_probability=max_keep_probability,
        confidence=confidence,
    )


@dataclass(frozen=True)
class TitleScreenDecision:
    outcome: Literal["reject", "abstain"]
    reason: str
    keep_probability_upper_bound: float | None = None


def evaluate(
    title: str,
    prompt_hash: str,
    artifact: TitleScreenArtifact | None,
    *,
    model: str | None = None,
) -> TitleScreenDecision:
    if artifact is None:
        return TitleScreenDecision("abstain", "artifact_missing")
    if artifact.prompt_hash != prompt_hash:
        return TitleScreenDecision("abstain", "prompt_mismatch")
    if model != artifact.reference_model:
        return TitleScreenDecision("abstain", "reference_model_mismatch")
    normalized = normalize_title(title)
    if not normalized:
        return TitleScreenDecision("abstain", "title_missing")
    counts = artifact.counts.get(normalized)
    if counts is None or not counts.training_count:
        return TitleScreenDecision("abstain", "title_unseen_in_training")
    if counts.training_keeps:
        return TitleScreenDecision("abstain", "training_contains_keep")
    if not counts.held_out_count:
        return TitleScreenDecision("abstain", "held_out_missing")
    if counts.held_out_keeps:
        return TitleScreenDecision("abstain", "held_out_contains_keep")

    # P(zero keeps | p, n) = (1-p)**n. Invert at alpha to bound p.
    # Bonferroni covers all training-selected titles in this frozen artifact.
    # Independent representative held-out observations remain a data requirement,
    # not something unique posting identities or this formula can establish.
    alpha = (1 - artifact.confidence) / artifact.candidate_count
    upper_bound = -math.expm1(math.log(alpha) / counts.held_out_count)
    if upper_bound > artifact.max_keep_probability:
        return TitleScreenDecision("abstain", "insufficient_evidence", upper_bound)
    return TitleScreenDecision("reject", "held_out_rejection_supported", upper_bound)
