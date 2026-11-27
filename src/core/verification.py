from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.answers import VERIFY_INPUT_CHARS

EVIDENCE_VERSION = "verification-evidence-v1"
EVIDENCE_INSTRUCTIONS = (
    "Extract evidence from this job posting, not a time-dependent verdict. "
    "Use short verbatim quotes from the supplied text; null means unknown or absent. "
    "closure: explicitly_closed only when this particular posting says it is filled, "
    "closed, no longer available, or no longer accepting applications. A deadline, "
    "including one in the past, is never explicitly_closed. Access errors, login walls "
    "and captchas are unknown. Otherwise no_closure_signal. "
    "deadline: YYYY-MM-DD only for an explicitly stated complete calendar date including "
    "the year. Never infer a year, resolve ambiguous numeric dates, or use a start date. "
    "deadline_kind: hard for a firm application cutoff, minimum_window for wording such "
    "as 'at least until', unknown for unclear or conflicting dates, none if absent. "
    "deadline_quote must include the date and its qualifying wording. "
    "restricted: true only for required security clearance or citizenship, explicit "
    "no-sponsorship, or explicit F1 ineligibility. Preferences, application questions, "
    "E-Verify participation and generic employment eligibility are not restrictions. "
    "Use null if the page is unavailable or the evidence is ambiguous. "
    "Return closure_quote and restriction_quote only for affirmative evidence."
)


def build_evidence_input(content: str) -> str:
    return content[:VERIFY_INPUT_CHARS]


class VerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    closure: Literal["explicitly_closed", "no_closure_signal", "unknown"]
    closure_quote: str | None
    deadline: date | None
    deadline_kind: Literal["hard", "minimum_window", "unknown", "none"]
    deadline_quote: str | None
    restricted: bool | None
    restriction_quote: str | None


@dataclass(frozen=True, slots=True)
class DerivedVerification:
    closed: bool | None
    closed_reason: str | None
    restricted: bool | None
    restriction_reason: str | None


_MONTHS = {
    name.lower(): index
    for index, names in enumerate(
        (
            ("January", "Jan"),
            ("February", "Feb"),
            ("March", "Mar"),
            ("April", "Apr"),
            ("May",),
            ("June", "Jun"),
            ("July", "Jul"),
            ("August", "Aug"),
            ("September", "Sep", "Sept"),
            ("October", "Oct"),
            ("November", "Nov"),
            ("December", "Dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4})"
_DATE_PATTERNS = (
    re.compile(rf"\b(?P<month>{_MONTH})\s+{_DAY},?\s+{_YEAR}\b", re.I),
    re.compile(rf"\b{_DAY}\s+(?P<month>{_MONTH}),?\s+{_YEAR}\b", re.I),
    re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b"),
)
_MINIMUM_WINDOW = re.compile(r"\b(?:at least|minimum|no earlier than|until further notice)\b", re.I)
_EXPLICIT_CLOSURE = re.compile(
    r"\b(?:position (?:has been |is )?filled|"
    r"(?:job|position|posting) (?:is |has been )?(?:closed|no longer available)|"
    r"no longer accepting applications)\b",
    re.I,
)


def _quoted(quote: str | None, content: str) -> bool:
    return bool(quote and " ".join(quote.split()) in " ".join(content.split()))


def _dates(quote: str) -> set[date]:
    result = set()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(quote):
            month = match["month"].lower().rstrip(".")
            try:
                result.add(
                    date(
                        int(match["year"]),
                        int(month) if month.isdigit() else _MONTHS[month],
                        int(match["day"]),
                    )
                )
            except ValueError:
                continue
    return result


def derive_verification(
    evidence: VerificationEvidence, content: str, *, as_of: datetime
) -> DerivedVerification:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("evaluation time must have a timezone")
    utc_day = as_of.astimezone(UTC).date()
    reason = None
    closed: bool | None = None
    if evidence.closure == "explicitly_closed":
        if (
            _quoted(evidence.closure_quote, content)
            and evidence.closure_quote
            and _EXPLICIT_CLOSURE.search(evidence.closure_quote)
        ):
            closed, reason = True, evidence.closure_quote
    elif evidence.closure == "no_closure_signal":
        if evidence.deadline_kind in {"none", "minimum_window"}:
            closed = False
        elif evidence.deadline_kind == "hard" and evidence.deadline is not None:
            quote = evidence.deadline_quote
            if _quoted(quote, content) and quote:
                if _MINIMUM_WINDOW.search(quote):
                    closed = False
                elif _dates(quote) == {evidence.deadline}:
                    # A date without a timezone can remain current on the next
                    # UTC day. Keep that boundary unknown rather than inventing
                    # a midnight cutoff in the server's timezone.
                    if utc_day <= evidence.deadline:
                        closed = False
                    elif utc_day - evidence.deadline > timedelta(days=1):
                        closed, reason = True, quote
    restricted = evidence.restricted
    restriction_reason = None
    if restricted:
        if _quoted(evidence.restriction_quote, content):
            restriction_reason = evidence.restriction_quote
        else:
            restricted = None
    return DerivedVerification(closed, reason, restricted, restriction_reason)
