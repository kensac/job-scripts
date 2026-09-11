"""A proportion that refuses to be a bare number.

Two surfaces now report rates over small samples - board analytics and
per-company response rates - and a second copy of this shape is how they drift
into disagreeing about what "below the floor" means. One definition, imported.
"""

from __future__ import annotations

from pydantic import BaseModel

# A proportion needs enough trials before it carries information. Thirty is the
# conventional floor for the normal approximation to the binomial: below it the
# Wald interval stops covering, and a single extra observation moves the rate
# by whole percentage points. It is a policy rather than a constant of nature,
# so callers can raise or lower it per request.
DEFAULT_MIN_SAMPLE = 30


class Rate(BaseModel):
    """A proportion carrying the counts that produced it.

    `value` is null below the floor and the caller renders "2 of 7", so the
    numerator and denominator are always present. `below_floor` says which
    kind of null a null value is: too small a sample, or no trials at all.
    """

    value: float | None
    numerator: int
    denominator: int
    below_floor: bool


def rate(numerator: int, denominator: int, min_sample: int) -> Rate:
    below = denominator < min_sample
    return Rate(
        value=None if below or not denominator else round(numerator / denominator, 4),
        numerator=numerator,
        denominator=denominator,
        below_floor=below,
    )
