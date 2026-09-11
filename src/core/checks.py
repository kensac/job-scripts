"""The posting checks: what the model is asked, what it must answer, and how
a verdict is read out of the answer.

THE REGISTRY IS THE ONLY PLACE A CHECK IS DEFINED. Adding one is an entry in
POSTING_CHECKS, not an arm on a dispatch ladder. There were two such ladders,
in routers/jobs.py and routers/admin.py, thirty lines each, the same shape,
and they had already drifted: admin grew a reason-less response schema and a
second addressing mode that jobs never got.

The prompt, the response model and the reader are one set per check; a caller
holding one without the others cannot run the check. That is why they are
together, and why the entry carries the reader rather than leaving each
caller to write the same lambda.

Not every check is a static entry. A custom filter's instructions are built
per filter at request time, so `custom` is a family addressed by prompt hash
rather than a row here. See core/filters.py.
"""

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
    """One check over a posting page.

    `name` is the check_type written to ai_queries, and the string every
    reader of that column compares against. It is here so that comparing
    against it is an attribute lookup rather than a literal.

    `verdict_of` reads (rejected, reason) out of a parsed response, which is
    the only shape the recording paths take.

    `terse_model` answers the same question without a reason. None means the
    check has no cheaper form.
    """

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
