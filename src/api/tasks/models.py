"""Structured-output schemas and instruction text for the AI checks."""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobExtract(BaseModel):
    company: str
    title: str
    locations: list[str]
    terms: list[str]


class FilterVerdict(BaseModel):
    """Evidence remains required for both live and batch decisions.

    Presentation guidance belongs in the output schema: changing the instruction
    builder would invalidate persisted verdict hashes without changing criteria.
    Do not truncate or reject longer historical reasons on read.
    """

    should_filter: bool
    reason: str = Field(
        description=(
            "One short clause naming the deciding criterion and its evidence. "
            "For missing evidence, name what is unstated; preserve the configured ambiguity policy. "
            "Do not repeat the verdict, list every criterion, or add introductory wording. "
            "Keep necessary qualifiers and negation; use at most 25 words."
        )
    )


class JobClosedVerdict(BaseModel):
    is_closed: bool
    reason: str


class VerifyVerdict(BaseModel):
    """One call, two independent axes, written as two verdict rows - so the
    reasons are separate fields rather than one shared sentence that would be
    ambiguous about which axis it explains."""

    is_closed: bool
    closed_reason: str
    requires_clearance_or_restrictions: bool
    clearance_reason: str


_VERIFY_INSTRUCTIONS = (
    "Evaluate this job posting on two independent axes.\n"
    "is_closed: true ONLY on posting-specific signals (no longer available/accepting, "
    "position filled, expired, deadline passed, job not found, 404). Site-wide errors, "
    "captchas, access blocks, or login walls say nothing about the job: false. "
    "Ambiguous: false.\n"
    "requires_clearance_or_restrictions: true ONLY for explicit restrictions: required "
    "security clearance or citizenship (US citizen required, US Person, Secret/TS-SCI/"
    "Public Trust), explicit no-sponsorship ('will not sponsor', 'no H1B'), or F1-not-"
    "eligible. Do NOT flag preferences, sponsorship offered, or application questions. "
    "When in doubt: false.\n"
    "closed_reason / clearance_reason: <=20 words each, citing the specific text that "
    "decided that axis. They are read when a human asks why a posting was ruled out, so "
    "quote the signal rather than restating the verdict."
)
