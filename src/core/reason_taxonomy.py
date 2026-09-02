"""Grouping for model-written rejection reasons.

The reasons are free prose, so near-duplicates ("salary not disclosed" /
"pay not listed" / "compensation not stated") have to land in one bucket or an
aggregate over them says nothing. This groups on keyword sets rather than
embeddings: measured against the full corpus of 14,084 custom rejections it
places 95.4%, which is enough that the cost and opacity of an embedding model
buys very little. `test_reason_taxonomy_coverage` pins that so a future edit
cannot quietly regress it.

Two properties of this data drive the shape of everything downstream:

A reason usually cites more than one thing - "16 years experience; not
appropriate for a new grad, plus no pay information" is both a seniority and a
missing-pay rejection. Groups therefore OVERLAP and their counts sum to more
than the number of rejections (21,542 over 14,084 corpus-wide). Nothing may
render these as parts of a whole.

Whether the posting supplied the information at all is not a topic like the
others - it is a property of the reasoning, and a rejection for an absent
salary is a different kind of event from one for a salary that was stated and
too low. It is kept out of GROUPS deliberately so it cannot be sorted into the
same ranked list and compared against them.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Group(NamedTuple):
    key: str
    label: str
    pattern: re.Pattern[str]


def _c(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


GROUPS: tuple[Group, ...] = (
    Group(
        "pay_undisclosed",
        "Pay not disclosed",
        _c(
            r"\bno\s+(disclosed|stated|listed|published|posted)\b"
            r"|\b(no|not|without|lacks?|missing)\b[^.;]{0,25}\b(pay|salary|comp\w*|wage|tc)\b"
            r"|\b(pay|salary|comp\w*|wage|tc)\b[^.;]{0,30}"
            r"\b(not|un|missing|absent)\s*"
            r"(disclos|listed|stated|specified|provided|given|published|available)?"
            r"|undisclosed|unclear (total )?comp"
            r"|cannot (confirm|verify|determine)[^.;]{0,40}"
            r"(pay|comp|salary|tc|\$|threshold|tier|bar a)"
        ),
    ),
    Group(
        "pay_below",
        "Pay below threshold",
        _c(
            r"below|under\s*\$|less than|tops? (out )?at|beneath|short of|well under"
            r"|<\s*~?\$?\s*\d|\(\s*<|does not (meet|reach)|fails? the \$"
            r"|bar a (fail|:)|annualizes? to|requires? ≥"
        ),
    ),
    Group(
        "not_engineering",
        "Not an engineering role",
        _c(
            r"non-?(engineering|tech|software)|not (a |an )?(software|engineering|tech)"
            r"|lacks? [^.;]{0,40}(backend|software|engineering|systems)"
            r"|minimal (software|engineering|overlap)|bar b (fail|:)"
            r"|(low|unclear|little|no) [^.;]{0,15}overlap|not aligned|not matching"
        ),
    ),
    Group(
        "seniority",
        "Seniority or experience mismatch",
        _c(
            # \u2013 is an en dash: the model writes ranges as "2-5 years" and
            # "2\u20135 years" interchangeably, so both have to match.
            r"\d+\+?\s*(-|to|\u2013)?\s*\d*\s*years?\s+(of\s+)?experience"
            r"|senior|not entry|new.?grad|first.?year|mid-?level"
            r"|staff engineer|principal"
        ),
    ),
    Group("education", "Education requirement", _c(r"\bph\.?d\b|master'?s|doctorate|mba\b")),
    Group(
        "location",
        "Location or work authorisation",
        _c(
            r"outside (the )?us|not us|non-?us|onsite|on-site"
            r"|relocat|visa|abroad|international"
        ),
    ),
    Group(
        "stack_mismatch",
        "Tech stack mismatch",
        _c(
            r"candidate (lacks|has no|shows|profile)|no [a-z/]+ (experience|background)"
            r"|mismatch|different (stack|domain)|too vague about tech"
        ),
    ),
    Group(
        "company_tier",
        "Company tier",
        _c(r"elite tier|top-?tier|not .{0,20}tier|company not"),
    ),
    Group(
        "employer_type",
        "Employer type",
        _c(
            r"staffing|recruiting agency|recruitment|consultanc"
            r"|not a direct employer|non-?direct employer|agency role"
        ),
    ),
)

GROUP_KEYS: tuple[str, ...] = tuple(g.key for g in GROUPS)
GROUP_LABELS: dict[str, str] = {g.key: g.label for g in GROUPS}


# Phrases that mark a rejection as resting on information the posting never
# supplied, rather than on information it supplied that fell short. Kept as
# data because the endpoint publishes it: a caller rendering this number has
# to be able to say what it means, and a regex over model prose is a heuristic
# that should read as one.
EVIDENCE_MISSING_PHRASES: tuple[str, ...] = (
    "undisclosed",
    "not disclosed",
    "not stated",
    "not listed",
    "not specified",
    "no pay",
    "no salary",
    "no compensation",
    "no wage",
    "missing",
    "cannot confirm",
    "cannot verify",
    "cannot determine",
    "unclear",
    "unlikely to",
    "no information",
)

EVIDENCE_MISSING_DESCRIPTION = (
    "Reason text uses language indicating the posting did not supply the "
    "information the filter needed, rather than supplying it and falling short."
)

_EVIDENCE_MISSING = _c("|".join(re.escape(p) for p in EVIDENCE_MISSING_PHRASES))


def classify(reason: str | None) -> tuple[str, ...]:
    """Every group whose pattern the reason matches, in GROUPS order.

    Empty means the reason matched nothing and belongs in the residual bucket,
    which callers must surface rather than drop - it is ~4.6% of the corpus.
    """
    if not reason:
        return ()
    return tuple(g.key for g in GROUPS if g.pattern.search(reason))


def is_evidence_missing(reason: str | None) -> bool:
    return bool(reason) and bool(_EVIDENCE_MISSING.search(reason))
