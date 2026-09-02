"""Structured-output schemas and instruction text for the AI checks."""

from __future__ import annotations

from pydantic import BaseModel


class JobExtract(BaseModel):
    company: str
    title: str
    locations: list[str]
    terms: list[str]


class FilterVerdict(BaseModel):
    """The verdict shape for every custom-filter call, batched or live.

    `reason` was dropped from the batched path for a while on the reasoning
    that reason text costs output tokens and is only read when a human debugs
    one job. Priced afterwards, that saving was about eleven cents a month -
    and it cost the ability to answer "why is my board empty", because the
    batch path is where scheduled work goes, so 100% of new verdicts recorded
    no reason at all. The tokens are worth it; the argument was qualitative
    and the number was never taken.

    build_custom_instructions already asks for "<=25 words citing the deciding
    factor", so this field is what the model was being told to produce and the
    schema was silently discarding. Restoring it changes no instruction text,
    which matters: prompt_hash is computed over those instructions, and
    altering them would fork every custom verdict ever recorded.
    """

    should_filter: bool
    reason: str


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
    "requires_clearance_or_restrictions: true ONLY for explicit restrictions — required "
    "security clearance or citizenship (US citizen required, US Person, Secret/TS-SCI/"
    "Public Trust), explicit no-sponsorship ('will not sponsor', 'no H1B'), or F1-not-"
    "eligible. Do NOT flag preferences, sponsorship offered, or application questions. "
    "When in doubt: false.\n"
    "closed_reason / clearance_reason: <=20 words each, citing the specific text that "
    "decided that axis. They are read when a human asks why a posting was ruled out, so "
    "quote the signal rather than restating the verdict."
)
