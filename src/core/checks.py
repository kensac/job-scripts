"""The registry of posting checks and their answer contracts.

Each POSTING_CHECKS entry owns its prompt, response model, and verdict reader.
Callers use the entry together instead of defining separate dispatch rules.
Custom filters build instructions per request and use a prompt hash, so they
are not static registry entries. See core/filters.py."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

CLOSED_INSTRUCTIONS = """Decide if a job posting is closed or still accepting applications.

Set is_closed=true ONLY on signals about THIS posting: "no longer available/accepting applications", "position filled", "posting expired", "deadline passed", "no longer open", "job not found", "404"/"page not found".
Set is_closed=false for anything that is a fetch problem rather than a posting status: site-wide errors (500/502/503, "service unavailable", maintenance pages), access blocks (captcha, "access denied", "are you a robot", login walls, geo/region restrictions, rate limiting). These say nothing about the job.
Otherwise is_closed=false. If job details are present or it is ambiguous, default to false (avoid false positives).

reason: <=15 words; quote the exact closed phrase when found. Be decisive, no hedging."""


class JobClosedResponse(BaseModel):
    is_closed: bool = Field(description="Whether the job posting is closed or no longer available")
    reason: str | None = Field(None, description="Brief explanation if job is closed")


CLEARANCE_INSTRUCTIONS = """Flag job postings that disqualify international candidates via explicit restrictions.

Set requires_clearance_or_restrictions=true ONLY for explicit restrictions, and set restriction_type:
- security_clearance: required clearance or citizenship ("US citizen required", "US Person", "Secret/Top Secret/TS-SCI/Public Trust").
- visa_sponsorship: "will not / does not sponsor", "no H1B sponsorship", "must be authorized to work without sponsorship".
- f1_restriction: "F1 not eligible/accepted/considered".

Do NOT flag: preferences ("clearance preferred"), sponsorship offered, application questions ("will you require sponsorship?"), or absent mentions. When in doubt, set false (prefer false negatives). If several apply, pick the most restrictive type.

reason: <=20 words, quote the phrase."""


class ClearanceRequirementResponse(BaseModel):
    requires_clearance_or_restrictions: bool = Field(
        description="Whether job requires security clearance, citizenship, or has visa/sponsorship restrictions"
    )
    restriction_type: (
        Literal["security_clearance", "citizenship", "visa_sponsorship", "f1_restriction"] | None
    ) = Field(None, description="Type of restriction if any")
    reason: str | None = Field(None, description="Brief explanation of the restriction")


class JobClosedVerdict(BaseModel):
    """The closed check without its reason field.

    The batched sweep asks for this one: it settles thousands of postings at a
    time and the reason is only read when a person opens one. The admin
    re-check offers both, so the caller picks per request.
    """

    is_closed: bool
    reason: str


@dataclass(frozen=True)
class PostingCheck:
    """One posting check and its persisted answer name.

    `name` is ai_queries.check_type. `verdict_of` returns the rejection flag
    and reason from a parsed response. `terse_model`, when present, answers
    without a reason."""

    name: str
    instructions: str
    response_model: type[BaseModel]
    verdict_of: Callable[[Any], tuple[bool, str]]
    terse_model: type[BaseModel] | None = None

    def model_for(self, with_reason: bool) -> type[BaseModel]:
        if with_reason or self.terse_model is None:
            return self.response_model
        return self.terse_model


CLOSED = PostingCheck(
    name="closed",
    instructions=CLOSED_INSTRUCTIONS,
    response_model=JobClosedResponse,
    terse_model=JobClosedVerdict,
    verdict_of=lambda p: (p.is_closed, getattr(p, "reason", "") or ""),
)

CLEARANCE = PostingCheck(
    name="clearance",
    instructions=CLEARANCE_INSTRUCTIONS,
    response_model=ClearanceRequirementResponse,
    verdict_of=lambda p: (
        p.requires_clearance_or_restrictions,
        p.reason or (p.restriction_type or ""),
    ),
)

# The checks a posting page is judged by, in the order a sweep runs them.
POSTING_CHECKS: dict[str, PostingCheck] = {c.name: c for c in (CLOSED, CLEARANCE)}

# Bind this tuple for queries that mean every registered check. A query that
# spells out the names would omit checks added later.
# Queries for one check remain literal. board_eligibility.STRUCTURAL also names
# both checks deliberately: each has a different visibility policy. Expanding
# that policy to every registered check would change who sees a posting.
POSTING_CHECK_NAMES: tuple[str, ...] = tuple(POSTING_CHECKS)
