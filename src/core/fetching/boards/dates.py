"""Interpret the dates published by listing boards."""

from __future__ import annotations

import datetime
import re

# --- dates -------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DAY = 86400


def posted_ts(text: str, now: datetime.datetime | None = None) -> int:
    """Epoch seconds for the ways boards write a posting date, or 0 when the
    text does not say. 0 is "unknown", and the catalog stores it as NULL.

    "Posted 30+ Days Ago" is unknown too: Workday says only that it is older
    than the window, and the hourly cycle sees every posting inside the window
    on its first appearance anyway, so the only loss is the backfill when a
    board is first added.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    t = text.strip().lower()
    if t in ("today", "posted today"):
        return int(now.timestamp())
    if t in ("yesterday", "posted yesterday"):
        return int(now.timestamp()) - _DAY
    # "5d", "2mo", "posted 14 days ago"; the unit is its first letter except
    # months, which must not read as minutes.
    m = re.match(r"(?:posted\s+)?(\d+)(\+?)\s*(mo|[hdwy])", t)
    if m:
        if m.group(2):
            return 0
        amount, unit = int(m.group(1)), m.group(3)
        days = {
            "h": amount / 24,
            "d": amount,
            "w": amount * 7,
            "mo": amount * 30,
            "y": amount * 365,
        }
        return int(now.timestamp() - days[unit] * _DAY)
    m = re.match(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?$", t)
    if m and m.group(1) in _MONTHS:
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            day = datetime.datetime(year, _MONTHS[m.group(1)], int(m.group(2)), tzinfo=datetime.UTC)
        except ValueError:
            return 0
        # A day without a year is the most recent one that is not in the
        # future; a feed updated daily never writes tomorrow.
        if not m.group(3) and day > now + datetime.timedelta(days=1):
            day = day.replace(year=year - 1)
        return int(day.timestamp())
    return 0
