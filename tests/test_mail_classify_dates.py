from __future__ import annotations

import datetime

import pytest

from tasks import mail_classify

SENT = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)


# A forward-looking kind, so the yearless cases below roll into next year.
# Which kind is used is load-bearing now and no longer incidental.
FUTURE = "interview_invite"


@pytest.mark.parametrize(
    ("raw", "expected_date", "expected_inferred"),
    [
        # ISO, which the instruction asks for.
        ("2026-09-08", datetime.date(2026, 9, 8), False),
        ("2026-09-08T17:00:00Z", datetime.date(2026, 9, 8), False),
        # Prose with a year. Measured over 200 real messages, the model
        # returns these far more often than ISO, and accepting only ISO threw
        # away 7.5% of the deadlines it found.
        ("March 1, 2023", datetime.date(2023, 3, 1), False),
        ("Thursday, February 1, 2024", datetime.date(2024, 2, 1), False),
        ("Tue Dec 10, 2024 at 4:00PM (EST)", datetime.date(2024, 12, 10), False),
        ("May 30, 2024, 5:00 PM Pacific Time", datetime.date(2024, 5, 31), False),
        # No year: resolved against the message and MARKED inferred.
        ("Jan. 15", datetime.date(2027, 1, 15), True),
        # Ordinal suffixes. Common in this corpus and, without consuming them,
        # the day fails to terminate and the whole date is dropped - found by
        # sampling 100 real messages after the first fix shipped.
        ("June 15th", datetime.date(2027, 6, 15), True),
        ("March 1st, 2023", datetime.date(2023, 3, 1), False),
        ("Dec 3rd 2024", datetime.date(2024, 12, 3), False),
        ("April 22nd, 2025", datetime.date(2025, 4, 22), False),
        ("November 1", datetime.date(2026, 11, 1), True),
        # No resolvable date at all.
        ("Tuesday at 2:00 PM", None, False),
        (None, None, False),
        ("", None, False),
        # The value that failed a 1,200-message task in production.
        ("2022-09-??", None, False),
        ("soon", None, False),
        ("2026-13-45", None, False),
        ("February 30, 2024", None, False),
    ],
)
def test_deadline_parsing(raw, expected_date, expected_inferred):
    got = mail_classify.parse_when(raw, sent_at=SENT, kind=FUTURE)
    assert (got.at.date() if got else None) == expected_date
    assert (got.year_inferred if got else False) is expected_inferred
    if got is not None:
        assert got.at.tzinfo is not None


def test_a_yearless_date_without_a_message_date_is_dropped():
    """Resolving it against TODAY would attach a deadline to an archived 2022
    email based on when the classifier happened to run."""
    assert mail_classify.parse_when("Jan. 15", kind=FUTURE) is None


def test_a_yearless_date_rolls_forward_rather_than_backward():
    """ "Jan. 15" in a December email means the following January. Resolving to
    the message's own year would put the deadline before the email."""
    december = datetime.datetime(2026, 12, 20, tzinfo=datetime.UTC)
    got = mail_classify.parse_when("Jan. 15", sent_at=december, kind=FUTURE)
    assert got is not None and got.at.date() == datetime.date(2027, 1, 15)
    assert got.year_inferred is True


def test_a_yearless_date_does_not_roll_forward_in_a_backward_looking_email():
    """A bare date in a rejection is the day you APPLIED, not a deadline next
    year. Rolling it forward manufactures a future date out of a past event,
    and the roll is an allowlist so a kind added later cannot inherit it by
    accident."""
    december = datetime.datetime(2026, 12, 20, tzinfo=datetime.UTC)
    for kind in ("rejection", "acknowledgement", "position_closed", None):
        got = mail_classify.parse_when("Jan. 15", sent_at=december, kind=kind)
        assert got is not None and got.at.date() == datetime.date(2026, 1, 15), kind


def test_a_stated_time_with_a_zone_resolves_to_the_real_instant():
    """Before this, every one of the 1,356 stored deadlines was exactly
    00:00 UTC - including those from "Tue Dec 10, 2024 at 4:00PM (EST)", whose
    true instant is 21:00 UTC. Twenty-one hours out, and looking healthy."""
    got = mail_classify.parse_when(
        "Tue Dec 10, 2024 at 4:00PM (EST)", sent_at=SENT, kind="interview_scheduled"
    )
    assert got is not None
    assert got.at == datetime.datetime(2024, 12, 10, 21, 0, tzinfo=datetime.UTC)
    assert got.is_instant is True


def test_zones_are_resolved_by_date_not_by_a_fixed_offset():
    """ "Pacific Time" is PDT in June and PST in December. A fixed -08:00 would
    be an hour out for half the year, so the label maps to an IANA zone and
    the date decides the offset."""
    summer = mail_classify.parse_when(
        "June 10, 2024 at 5:00 PM Pacific Time", sent_at=SENT, kind="interview_scheduled"
    )
    winter = mail_classify.parse_when(
        "December 10, 2024 at 5:00 PM Pacific Time", sent_at=SENT, kind="interview_scheduled"
    )
    assert summer is not None and winter is not None
    assert summer.at.hour == 0  # 17:00 PDT -> 00:00 UTC next day
    assert winter.at.hour == 1  # 17:00 PST -> 01:00 UTC next day


def test_a_time_without_a_zone_keeps_the_date_and_drops_the_clock():
    """Containers run America/New_York and Postgres runs UTC, so an unzoned
    clock time is not resolvable. Storing it as UTC would be a silently wrong
    instant rather than a missing one."""
    got = mail_classify.parse_when("June 15th at 9:00 AM", sent_at=SENT, kind="interview_scheduled")
    assert got is not None
    assert got.at == datetime.datetime(2027, 6, 15, 0, 0, tzinfo=datetime.UTC)
    assert got.is_instant is False


def test_a_naive_iso_timestamp_is_treated_the_same_way():
    """The one branch that looked precise enough to get away with assuming
    UTC. It carries a clock and no zone, exactly like the prose case."""
    got = mail_classify.parse_when("2026-09-08T17:00:00", sent_at=SENT, kind=FUTURE)
    assert got is not None
    assert got.at == datetime.datetime(2026, 9, 8, 0, 0, tzinfo=datetime.UTC)
    assert got.is_instant is False


def test_an_offset_bearing_iso_timestamp_keeps_its_instant():
    got = mail_classify.parse_when("2026-09-08T17:00:00-05:00", sent_at=SENT, kind=FUTURE)
    assert got is not None
    assert got.at == datetime.datetime(2026, 9, 8, 22, 0, tzinfo=datetime.UTC)
    assert got.is_instant is True


def test_only_a_scheduled_interview_is_an_appointment():
    """An interview that has been scheduled has a time. Everything else states
    a date to act by - including "submit by March 1, 5:00 PM Pacific", which
    carries a clock and is still a deadline."""
    assert mail_classify.is_appointment("interview_scheduled") is True
    for kind in ("interview_invite", "assessment_invite", "offer", "rejection", None):
        assert mail_classify.is_appointment(kind) is False
