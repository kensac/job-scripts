"""A proportion that refuses to be a bare number.

Two surfaces now report rates over small samples - board analytics and
per-company response rates - and a second copy of this shape is how they drift
into disagreeing about what "below the floor" means. One definition, imported.
"""

from __future__ import annotations

from typing import Any

# A proportion needs enough trials before it carries information. Thirty is the
# conventional floor for the normal approximation to the binomial: below it the
# Wald interval stops covering, and a single extra observation moves the rate
# by whole percentage points. It is a policy rather than a constant of nature,
# so callers can raise or lower it per request.
DEFAULT_MIN_SAMPLE = 30


def rate(numerator: int, denominator: int, min_sample: int) -> dict[str, Any]:
    """Below the floor `value` is None and the caller renders "2 of 7"; the
    numerator and denominator are always present so it can."""
    below = denominator < min_sample
    return {
        "value": None if below or not denominator else round(numerator / denominator, 4),
        "numerator": numerator,
        "denominator": denominator,
        "below_floor": below,
    }
