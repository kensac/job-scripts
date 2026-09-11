"""The catalog's posting type, and the date floor ingest reads.

Every board fetcher in core/boards.py returns this, so it is what ingest, the
catalog and the checks all agree on. It lived in the sheet-era pipeline until
that module was retired; nothing about it was sheet-specific.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

# A posting whose board gives no date is treated as posted on this day. The
# floor moves forward when the catalog is old enough that older postings are
# noise rather than history.
FALLBACK_CUTOFF_DATE = "2026-05-01"
FALLBACK_CUTOFF_TS: int = int(datetime.datetime.fromisoformat(FALLBACK_CUTOFF_DATE).timestamp())


@dataclass(frozen=True)
class JobPosting:
    company: str
    locations: list[str]
    title: str
    url: str
    terms: list[str]
    active: bool
    date_posted: int
    raw_url: str = ""
    # The posting's own text when the board's listing call carries it
    # (Greenhouse, Lever, Ashby); empty means fetch the page. Stored on the
    # listing so nothing has to be scraped twice.
    description: str = ""
    # The listing record as the board returned it, minus the text fields
    # above, for backtests that need a field nobody mapped yet.
    raw: dict | None = None
