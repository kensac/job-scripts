"""Parse stated mail dates and distinguish appointments from deadlines."""

from __future__ import annotations

import dataclasses
import datetime
import re
import zoneinfo

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
_MONTHS.update({m[:3]: i for m, i in _MONTHS.items()})

# The ordinal suffix is optional and must be consumed: "June 15th" is
# extremely common in this corpus and without it the day fails to terminate,
# so the whole date was dropped.
#
# This is a search rather than a match, which is why weekday prefixes
# ("Thursday, February 1, 2024") and trailing text ("at 4:00PM (EST)") never
# broke it. What they DID break is silently worse - see _TIME_RE.
_DATE_RE = re.compile(
    r"\b(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,)?(?:\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)

# The time was thrown away on every row. Before this, every one of the 1,356
# stored deadlines was exactly 00:00 UTC, including the ones that came from
# "Tue Dec 10, 2024 at 4:00PM (EST)" - a real instant of 21:00 UTC recorded 21
# hours early, and looking entirely healthy. A wrong timestamp that parses is
# worse than one that does not, so the time is captured now.
_TIME_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\b",
    re.IGNORECASE,
)

# Labels mapped to IANA ZONES rather than fixed offsets, deliberately. "Pacific
# Time" in June is PDT and in December is PST; a fixed -08:00 would be an hour
# out for half the year. zoneinfo resolves it from the date itself.
#
# "EST" written in July is treated the same way - as Eastern, not as a literal
# -05:00 - because people write the standard-time abbreviation year round and
# mean the zone. That is a judgement call and it is the one that is right more
# often on this corpus.
_ZONES = {
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "eastern": "America/New_York",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "central": "America/Chicago",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "mountain": "America/Denver",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "utc": "UTC",
    "gmt": "UTC",
}
_ZONE_RE = re.compile(
    r"\b(" + "|".join(sorted(_ZONES, key=len, reverse=True)) + r")\b(?:\s+time)?",
    re.IGNORECASE,
)

# Only these roll a bare "Jan. 15" forward into next year. A date without a
# year in a rejection or an acknowledgement is almost always the date you
# APPLIED, and rolling it forward invents a future deadline out of a past
# event. The list is an allowlist rather than a blocklist so that a kind added
# later does not silently inherit the roll.
_FUTURE_FACING_KINDS = frozenset(
    {
        "assessment_invite",
        "interview_invite",
        "interview_scheduled",
        "info_request",
        "offer",
        "recruiter_outreach",
    }
)

# An interview that has been scheduled has a TIME, not a deadline. Everything
# else states a date you must act by. Splitting on kind rather than on "did we
# find a time" is what distinguishes an appointment from "submit by March 1,
# 5:00 PM Pacific", which is a deadline that happens to carry a clock time.
_APPOINTMENT_KINDS = frozenset({"interview_scheduled"})


@dataclasses.dataclass(frozen=True)
class ParsedWhen:
    """A moment an email referred to, and how much of it was actually stated.

    `at` is always aware UTC. `is_instant` says whether it is a real moment or
    only a date: a stated time with no stated zone is NOT resolvable, because
    the fleet runs containers on America/New_York while Postgres is UTC, so
    picking either would be a coin flip dressed as a fact. Such a value keeps
    the date and drops the clock rather than assuming an offset.
    """

    at: datetime.datetime
    is_instant: bool
    year_inferred: bool


def _parse_time(text: str) -> tuple[int, int] | None:
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if not (1 <= hour <= 12) or minute > 59:
        return None
    meridiem = match.group("meridiem").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return hour, minute


def _parse_zone(text: str) -> zoneinfo.ZoneInfo | None:
    match = _ZONE_RE.search(text)
    if not match:
        return None
    try:
        return zoneinfo.ZoneInfo(_ZONES[match.group(1).lower()])
    except Exception:
        return None


def parse_when(
    value: str | None,
    *,
    sent_at: datetime.datetime | None = None,
    kind: str | None = None,
) -> ParsedWhen | None:
    """Parse a stated mail date without inventing an instant.

    Full dates resolve directly. Yearless dates use the message date and are
    marked inferred; only forward-looking kinds can select a future year.
    A time needs a zone to become an instant. Without a zone, keep the date
    and drop the time rather than storing a local time as UTC. Return None
    for phrases without a resolvable date; the caller retains the raw text.
    """
    if not value:
        return None
    text = value.strip()

    try:
        iso = datetime.datetime.fromisoformat(text)
    except ValueError:
        iso = None
    if iso is not None:
        if iso.tzinfo is not None:
            return ParsedWhen(iso.astimezone(datetime.UTC), True, False)
        # A naive ISO timestamp carries a clock with no zone, which is the same
        # unresolvable case as "9:00 AM" with no zone below. Keeping the date
        # and dropping the time is the consistent answer; reading it as UTC
        # would be inventing an offset in the one branch that looks precise.
        return ParsedWhen(
            datetime.datetime.combine(iso.date(), datetime.time.min, tzinfo=datetime.UTC),
            False,
            False,
        )

    match = _DATE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    day = int(match.group("day"))
    year_text = match.group("year")
    year_inferred = year_text is None
    if year_text is not None:
        year = int(year_text)
    elif sent_at is not None:
        year = sent_at.year
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:
            return None
        if candidate < sent_at.date() and (kind in _FUTURE_FACING_KINDS):
            year += 1
    else:
        return None

    try:
        resolved = datetime.date(year, month, day)
    except ValueError:
        return None

    clock = _parse_time(text)
    zone = _parse_zone(text) if clock else None
    if clock and zone is not None:
        local = datetime.datetime(resolved.year, resolved.month, resolved.day, *clock, tzinfo=zone)
        return ParsedWhen(local.astimezone(datetime.UTC), True, year_inferred)
    return ParsedWhen(
        datetime.datetime.combine(resolved, datetime.time.min, tzinfo=datetime.UTC),
        False,
        year_inferred,
    )


def is_appointment(kind: str | None) -> bool:
    return kind in _APPOINTMENT_KINDS
