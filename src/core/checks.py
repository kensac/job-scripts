"""The two posting checks: what the model is asked, and what it must answer.

The prompt and the response model are one pair per check; a caller that has
one without the other cannot run the check. Kept together for that reason.
"""

from __future__ import annotations

from typing import Literal

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
