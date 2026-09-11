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

from pydantic import BaseModel, ConfigDict

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


class CompExtract(BaseModel):
    """Standardised so the number is comparable across postings. Amounts stay
    exactly as advertised; normalisation to a yearly figure happens here, not
    in the model, so a bad period can be corrected without re-running the AI."""

    model_config = ConfigDict(allow_inf_nan=False)

    has_comp: bool
    comp_min: float | None = None
    comp_max: float | None = None
    currency: str = ""
    period: str = ""
    basis: str = ""
    display: str = ""


# Posting text beyond this point is not sent to the extraction model.
COMP_INPUT_CHARS = 20000


COMP_INSTRUCTIONS = (
    "Extract the advertised compensation for THIS job from the page content. "
    "has_comp=true only when a concrete pay amount or range is stated for this "
    "role; false for benefits, equity-only mentions, and salary-law boilerplate "
    "with no numbers.\n"
    "comp_min/comp_max: numeric bounds EXACTLY as advertised, never converted "
    "(26.44 for $26.44/hr, 120000 for $120k/yr, 2000 for $2,000 per week). "
    "Equal values when a single amount is given.\n"
    "period: EXACTLY one of hourly, daily, weekly, biweekly, semimonthly, "
    "monthly, yearly, one_time. Read it from the posting - do not guess from "
    "the size of the number. Use one_time for a stipend, signing bonus, or any "
    "lump sum that is not a recurring wage.\n"
    "basis: base for salary only, total for explicit total compensation or OTE, "
    "stipend for an internship or one-off stipend, unspecified if unclear.\n"
    "currency: ISO 4217 code, e.g. USD, CAD, GBP. Use USD only when the posting "
    "actually indicates US dollars.\n"
    "display: a compact human string as advertised, e.g. '$120k-$150k' or '$45/hr'."
)
