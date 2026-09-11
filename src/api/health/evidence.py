"""How a detector decides it has enough to say anything.

Every floor here exists because a rate below it is noise, and alerting on
noise trains you to ignore alerts. The confounds are the other half: a
comparison is only as good as the population it runs over."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# samples are not independent, see MAX_PER_COMPANY.
MIN_SAMPLES = 50
MIN_CONTENT_SAMPLES = 10

# One employer bulk-posting a batch of no-sponsorship roles is a single
# editorial decision, not N independent observations. Capping each company's
# contribution per window keeps a seasonal drop (Qorvo posted 36 internships in
# one day, 31 of them clearance-restricted) from moving a whole source's rate.
MAX_PER_COMPANY = 5

# A job whose first-ever check lands long after we catalogued it came from a
# backlog sweep, not the live feed. Those are systematically staler, and more
# often closed or restricted, than freshly-ingested postings, so mixing them
# in makes a coverage change look like breakage. This is the same confound
# #110 removed for re-checks, one level down: fetch_missing_content gives old
# jobs their FIRST check, so restricting to first-ever checks does not exclude
# it on its own.
FRESH_CHECK_WINDOW = "3 days"


def _pct(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def _int_from(column: str, key: str) -> str:
    """A jsonb value read as an integer, or NULL when it is not one.

    `(progress->>'total')::int` in a WHERE clause is not safe: Postgres does
    not promise to evaluate the filters before the cast, so one row anywhere in
    tasks whose total is not a number fails the whole query, and the detector
    that runs it reports detector_failed every hour instead of what it watches.
    The corpus generates exactly that shape - tests/corpus.py fills every json
    key with a random string - which is how this was found.

    CASE fixes the order, so a bad value reads as NULL and drops out of the
    comparison rather than raising.
    """
    return (
        f"CASE WHEN jsonb_typeof({column}->'{key}') = 'number' THEN ({column}->>'{key}')::int END"
    )


# A worker whose last report is older than this is not idle, it is gone, and
# the reaper's concern rather than this detector's. Two housekeeping ticks.
WORKER_FRESH = "2 minutes"
