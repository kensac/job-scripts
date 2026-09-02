"""Structured-output schemas and instruction text for the AI checks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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

    `basis` is anchored to the single deciding factor named in `reason`, not
    to the posting as a whole. A dry run against live postings showed why:
    these prompts carry several criteria, so "did the posting supply what the
    criteria needed" has no answer when it stated the pay and omitted the tech
    stack. Two near-identical postings came back with different values, one of
    them contradicting its own reason text. Tying it to the deciding factor
    makes it a question about one thing that is already named.

    It is steered by its own description here rather than by the prompt.
    Putting the guidance in build_custom_instructions would have been the
    obvious place and would have moved every prompt_hash - not immediately,
    which is the trap: hashes are stored on user_filters and only recomputed
    when a filter is patched, so the fork would fire later, on an unrelated
    edit like a rename or an enable toggle, orphaning that filter's history
    and triggering a paid re-run. Priced at the current rate that is $1.32 for
    the one enabled filter and $6.19 if all ten were touched, against $0.009 a
    month for the field itself. The schema is not part of the hash, so this
    costs the tokens and nothing else.
    """

    should_filter: bool
    reason: str
    basis: Literal["stated", "undetermined"] = Field(
        description=(
            "Describes the ONE deciding factor you named in `reason` - not the "
            "posting as a whole, and not your confidence. "
            "'stated': the posting explicitly contained that deciding factor "
            "(a salary figure, a named technology, a listed requirement) and "
            "your verdict follows from what it said. "
            "'undetermined': the posting never disclosed that deciding factor, "
            "or was too vague about it to tell, so the verdict follows from "
            "the ambiguity policy instead. "
            "A posting that states pay but omits the tech stack is 'stated' "
            "when pay decided it and 'undetermined' when the stack did."
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
    "requires_clearance_or_restrictions: true ONLY for explicit restrictions — required "
    "security clearance or citizenship (US citizen required, US Person, Secret/TS-SCI/"
    "Public Trust), explicit no-sponsorship ('will not sponsor', 'no H1B'), or F1-not-"
    "eligible. Do NOT flag preferences, sponsorship offered, or application questions. "
    "When in doubt: false.\n"
    "closed_reason / clearance_reason: <=20 words each, citing the specific text that "
    "decided that axis. They are read when a human asks why a posting was ruled out, so "
    "quote the signal rather than restating the verdict."
)
