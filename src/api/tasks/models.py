"""Structured-output schemas and instruction text for the AI checks."""

from __future__ import annotations

from pydantic import BaseModel


class JobExtract(BaseModel):
    company: str
    title: str
    locations: list[str]
    terms: list[str]


class FilterVerdict(BaseModel):
    should_filter: bool
    reason: str


class FilterVerdictLean(BaseModel):
    """Default verdict shape: no reason text — reasons cost output tokens on
    every call and are only read when a human debugs, so they're generated
    on demand via the explain endpoint instead."""

    should_filter: bool


class JobClosedLean(BaseModel):
    is_closed: bool


class VerifyLean(BaseModel):
    is_closed: bool
    requires_clearance_or_restrictions: bool


_VERIFY_INSTRUCTIONS = (
    "Evaluate this job posting on two independent axes.\n"
    "is_closed: true ONLY on posting-specific signals (no longer available/accepting, "
    "position filled, expired, deadline passed, job not found, 404). Site-wide errors, "
    "captchas, access blocks, or login walls say nothing about the job: false. "
    "Ambiguous: false.\n"
    "requires_clearance_or_restrictions: true ONLY for explicit restrictions — required "
    "security clearance or citizenship (US citizen required, US Person, Secret/TS-SCI/"
    "Public Trust), explicit no-sponsorship ('will not sponsor', 'no H1B'), or F1-not-"
    "eligible. Do NOT flag preferences, sponsorship offered, or application questions. "
    "When in doubt: false."
)
