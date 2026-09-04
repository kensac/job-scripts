"""The closed vocabularies a posting's requirements are recorded in.

Lives in core because the extraction and the API must not each keep their own
copy: the extraction validates the model's answer against them, and the market
endpoint orders its degree and clearance tables by POSITION in these tuples. A
model answering "Bachelor's degree" and one answering "bachelors" have to land
on the same token or the counts are split across two spellings of one level.
"""

from __future__ import annotations

# Ordered least to most, because degree and clearance requirements are FLOORS.
# A posting asking for "a Bachelor's or Master's" states a floor of bachelors,
# and one asking for "Secret or above" states a floor of secret. Ordering is
# what lets a reader see the market's floors as a ladder - array_position
# rather than alphabetical, which would put "bachelors" above "phd".
DEGREE_LEVELS = ("none", "high_school", "associate", "bachelors", "masters", "phd")
CLEARANCE_LEVELS = ("none", "public_trust", "confidential", "secret", "top_secret", "ts_sci")

# Unordered: a posting is one of these, not at least one of them. Seniority
# reads like a ladder but is not comparable in the same way - a staff engineer
# role is not "more than" a manager role, and treating it as one would drop
# postings out of a slice for a reason nobody stated.
SENIORITIES = (
    "intern",
    "new_grad",
    # Not in the first draft of this vocabulary. The pilot's model answered
    # "postdoc" for two of 60 postings, which is the corpus saying it holds a
    # category the list was missing rather than the model drifting - research
    # roles are a real slice of this market, and dropping them to NULL would
    # have hidden them instead of counting them.
    "postdoc",
    "entry",
    "mid",
    "senior",
    "staff",
    "principal",
    "manager",
    "executive",
)
EMPLOYMENT_TYPES = ("intern", "full_time", "part_time", "contract", "temporary")
SPONSORSHIPS = ("offered", "not_offered")

SKILL_KINDS = ("required", "preferred")


# A full working life, 18 to 68. Nothing above this can be a number of years of
# professional experience, so a larger value is the model having read a salary,
# a requisition number or a calendar year off the page. comp.py bounds its
# annual figure for the same reason: a wrong number in a sortable column is
# worse than a missing one, because it silently reorders every answer built on
# it, and nobody can see that it did.
MAX_PLAUSIBLE_YOE = 50


def in_vocabulary(value: str | None, allowed: tuple[str, ...]) -> str | None:
    """A vocabulary token, or None when the value is outside it.

    None and an out-of-vocabulary string both mean "we do not know", but only
    None says so to every reader. Letting 'Bachelors Degree' through would give
    the aggregate a bucket of one that looks like a finding.
    """
    text = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return text if text in allowed else None
