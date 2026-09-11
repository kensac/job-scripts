"""The closed vocabularies compensation is recorded in, and the multipliers
that make one number comparable to another.

Here rather than in `tasks/comp.py`, which extracts it, because the API
declares these in the shapes it returns and the services may not import the
handlers. A vocabulary is not a task.

The Literal is the definition and the tuple is derived from it, so a value the
extractor accepts and a value the schema declares cannot come apart.
"""

from __future__ import annotations

from typing import Literal, get_args

# Multipliers to a yearly figure. The old version knew only hourly and
# monthly, so a weekly wage was either stored raw ($5,000/week became
# $5,000/yr) or multiplied by 2080 ($2,000/week became $4,160,000/yr). Both
# shapes are in production data today, which is what makes the comp column
# unsortable. "one_time" is deliberately absent: a stipend or signing bonus
# has no annual equivalent and must not be invented.
PERIOD_TO_YEARLY: dict[str, float] = {
    "hourly": 2080.0,
    "daily": 260.0,
    "weekly": 52.0,
    "biweekly": 26.0,
    "semimonthly": 24.0,
    "monthly": 12.0,
    "yearly": 1.0,
}

CompPeriod = Literal[
    "hourly", "daily", "weekly", "biweekly", "semimonthly", "monthly", "yearly", "one_time"
]
COMP_PERIODS: tuple[CompPeriod, ...] = get_args(CompPeriod)

CompBasis = Literal["base", "total", "stipend", "unspecified"]
COMP_BASES: tuple[CompBasis, ...] = get_args(CompBasis)

assert set(PERIOD_TO_YEARLY) | {"one_time"} == set(COMP_PERIODS), (
    "every period either converts to a year or is deliberately excluded"
)
