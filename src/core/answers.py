"""The shapes a model must answer in.

Here rather than under `tasks` because both layers read them: a handler asks
the question, and a router reads the same shape back when a person asks why a
posting was ruled out. A shape is not behaviour, and reaching past a handler
for one was five of the six imports that stopped the layering being stated.

Structured-output schemas and instruction text for the AI checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class JobExtract(BaseModel):
    company: str
    title: str
    locations: list[str]
    terms: list[str]


class FilterDecision(BaseModel):
    should_filter: bool


class FilterResult(FilterDecision):
    """Read old paid responses without inventing evidence for decision-only calls."""

    reason: str | None = None


# This explanation schema retains its historical description verbatim because
# providers receive it in requests. The eleven-cent estimate below motivated
# the earlier reason-preserving policy; ordinary filtering is now explicitly
# decision-only. Keep that evidence without changing the explanation wire format.
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


class VerifyVerdict(BaseModel):
    """One call, two independent axes, written as two verdict rows - so the
    reasons are separate fields rather than one shared sentence that would be
    ambiguous about which axis it explains."""

    is_closed: bool
    closed_reason: str
    requires_clearance_or_restrictions: bool
    clearance_reason: str


# Posting text beyond this point is not sent to the verification model.
VERIFY_INPUT_CHARS = 20000


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


@dataclass(frozen=True, slots=True)
class VerificationRequestRecipe:
    instructions: str
    response_model: type[VerifyVerdict]
    input_chars: int
    max_output_tokens: int

    def build_input(self, content: str) -> str:
        return content[: self.input_chars]


VERIFICATION_REQUEST = VerificationRequestRecipe(
    instructions=_VERIFY_INSTRUCTIONS,
    response_model=VerifyVerdict,
    input_chars=VERIFY_INPUT_CHARS,
    max_output_tokens=1000,
)


# Verification inside the joint request writes a reason only for an axis it flags.
# A passed axis's reason ("no closure signals") was ~45 output tokens nobody reads:
# the board shows a reason only when it explains a rejection. Measured 2026-10-09
# against the joint request as before, same postings: closed 173 vs 174 of 300
# stored closures, clearance 65 vs 64, Tech New Grad keeps 151 vs 150 of 400,
# Aerospace 33 vs 32 (every paired sign test p=1.0); output 174 -> 127 tokens.
# The plain request keeps its wording, because reverify's unchanged-page reuse
# keys on that exact question.
_REASONS_ALWAYS = (
    "closed_reason / clearance_reason: <=20 words each, citing the specific text that "
    "decided that axis. They are read when a human asks why a posting was ruled out, so "
    "quote the signal rather than restating the verdict."
)
_JOINT_VERIFY_INSTRUCTIONS = _VERIFY_INSTRUCTIONS.replace(
    _REASONS_ALWAYS,
    "closed_reason / clearance_reason: when that axis is true, <=20 words citing the specific "
    "text that decided it; they are read when a human asks why a posting was ruled out, so "
    "quote the signal rather than restating the verdict. When that axis is false, an empty "
    "string.",
)
assert _JOINT_VERIFY_INSTRUCTIONS != _VERIFY_INSTRUCTIONS


_COUNTS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def joint_question_key(board_id: int) -> str:
    return f"board_{board_id}"


def joint_verification(board_criteria: dict[int, str]) -> tuple[str, type[BaseModel]]:
    """Verification and each board's decision about one posting, in one request.

    Every board reviews the same posting text that verification has just read,
    so asking separately paid for that text once per board. Measured on
    2026-10-07 against re-running today's separate requests on the same
    postings (live, gpt-6-luna, low effort): Tech New Grad kept 136 of 400
    joint vs 133 separate (sign test p=0.76), Aerospace 32 vs 29 (p=0.45),
    closed 173 vs 169 of 300 stored-closed (p=0.34) and clearance identical.
    Re-running the separate request itself flips 30% of past Tech New Grad
    keeps, so equality is judged against that, not against stored verdicts.

    `board_criteria` maps a board id to its criteria and ambiguity rule
    (core.filters.custom_criteria_instructions). The wording below is the one
    measured; changing it is a new measurement.
    """
    from pydantic import create_model

    keys = [joint_question_key(board_id) for board_id in sorted(board_criteria)]
    blocks = [f'<question name="verification">\n{_JOINT_VERIFY_INSTRUCTIONS}\n</question>'] + [
        f'<question name="{key}">\n{board_criteria[board_id]}\n</question>'
        for key, board_id in zip(keys, sorted(board_criteria), strict=True)
    ]
    count = len(blocks)
    instructions = (
        f"You will answer {_COUNTS.get(count, str(count))} independent questions about one job "
        "posting. Answer each exactly as if it were the only question; one answer must not "
        "influence another.\n\n"
        + "\n\n".join(blocks)
        + "\n\nReturn only a JSON object with verification (its four fields) and "
        + ", ".join(f"{key}.should_filter" for key in keys)
        + ". Do not include reasons for the should_filter answers."
    )
    fields: dict[str, Any] = {"verification": (VerifyVerdict, ...)}
    fields.update({key: (FilterDecision, ...) for key in keys})
    return instructions, create_model("JointVerification", **fields)


# The vocabulary the mail classifier must answer within. Read by the router
# that serves those kinds to a person, which is why it is not under `tasks`.
EVENT_KINDS = (
    "acknowledgement",
    "rejection",
    "assessment_invite",
    "interview_invite",
    "interview_scheduled",
    "info_request",
    "offer",
    "recruiter_outreach",
    "position_closed",
    "not_job_related",
)


# What a stranger's answer sounds like when nobody has said otherwise. A
# person overrides the whole thing from settings; this is not merged with
# theirs, it is replaced by it.
DEFAULT_STYLE = (
    "Concise but not abrupt: the shortest version that still has enough context to feel "
    "thoughtful. Natural and conversational, like something a person would actually type, "
    "not polished corporate language. Professional without being formal. Simple wording over "
    "jargon or buzzwords. Specific rather than generic: name the actual project, situation or "
    "reason instead of filler. Confident but understated: show competence through what was "
    "done and how, never by declaring it. Low fluff: no excessive gratitude, pleasantries or "
    "repetition."
)
