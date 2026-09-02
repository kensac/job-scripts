"""Per-company response rates and time-to-outcome.

The whole difficulty is sample size. With one user the median company has ONE
application, so almost every rate here is legitimately null and the counts
carry the information instead.
"""

from __future__ import annotations

import datetime

from api import db, rates
from api.routers.companies import _OUTCOME_KINDS, _RESPONSE_SQL, _response_block

OUTCOMES = list(_OUTCOME_KINDS)


def _apply(uid: int, company: str, *, provenance: str, applied_day: int) -> int:
    row = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance, applied_at) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (uid, company, provenance, datetime.datetime(2026, 1, applied_day, tzinfo=datetime.UTC)),
    )
    assert row is not None
    return row["id"]


def _reply(uid: int, app_id: int, kind: str, *, domain: str, day: int) -> None:
    msg = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at) VALUES (%s, %s, 'gmail', %s, 'x', %s) RETURNING id",
        (
            uid,
            f"<{app_id}-{kind}-{day}@x>",
            f"jobs@{domain}",
            datetime.datetime(2026, 1, day, tzinfo=datetime.UTC),
        ),
    )
    assert msg is not None
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s, %s, 'high')",
        (msg["id"], kind),
    )
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'company_name', 'high')",
        (msg["id"], app_id),
    )


def _block(key: str, min_sample: int, intermediaries: list[str] | None = None):
    rows = db.query(
        _RESPONSE_SQL,
        {"keys": [key], "outcomes": OUTCOMES, "intermediaries": intermediaries or []},
    )
    return _response_block(rows[0] if rows else None, min_sample)


def test_a_rate_below_the_floor_is_null_but_keeps_its_counts(f):
    """A company with three applications and one reply does not have a 33%
    response rate; it has one reply. The caller renders "1 of 3"."""
    uid = f.make_user()
    for day in (1, 2, 3):
        app = _apply(uid, "Acme", provenance="tracker", applied_day=day)
    _reply(uid, app, "rejection", domain="acme.test", day=10)

    block = _block("acme", rates.DEFAULT_MIN_SAMPLE)
    assert block is not None
    assert block["replied"]["value"] is None
    assert block["replied"]["numerator"] == 1
    assert block["replied"]["denominator"] == 3
    assert block["replied"]["below_floor"] is True


def test_the_floor_is_a_parameter_not_a_constant(f):
    """With one user almost nothing clears thirty - two companies out of 1,283
    - so the caller has to be able to lower it deliberately."""
    uid = f.make_user()
    for day in (1, 2, 3):
        app = _apply(uid, "Acme", provenance="tracker", applied_day=day)
    _reply(uid, app, "rejection", domain="acme.test", day=10)

    assert _block("acme", 3)["replied"]["value"] == round(1 / 3, 4)
    assert _block("acme", 4)["replied"]["value"] is None


def test_an_acknowledgement_is_a_reply_but_not_an_outcome(f):
    """Counting an autoresponder as an answer reports a perfect response rate
    for every company running an ATS."""
    uid = f.make_user()
    app = _apply(uid, "Acme", provenance="tracker", applied_day=1)
    _reply(uid, app, "acknowledgement", domain="acme.test", day=2)

    block = _block("acme", 1)
    assert block["replied"]["numerator"] == 1
    assert block["reached_outcome"]["numerator"] == 0


def test_timing_ignores_mail_derived_applications(f):
    """For all 1,783 mail-derived applications in production, applied_at IS the
    first matched message's sent_at, exactly. So a duration measured from it is
    the gap between the first mail and the first DECIDING mail - zero whenever
    a rejection arrives with nothing before it. It is not a slow measurement,
    it is not a measurement."""
    uid = f.make_user()
    app = _apply(uid, "Acme", provenance="email", applied_day=1)
    _reply(uid, app, "rejection", domain="acme.test", day=20)

    block = _block("acme", 1)
    assert block["reached_outcome"]["numerator"] == 1, "it still counts as an outcome"
    assert block["days_to_first_outcome"]["n"] == 0, "but it is not timed"
    assert block["days_to_first_outcome"]["median"] is None


def test_timing_uses_tracker_dates_and_reports_its_basis(f):
    uid = f.make_user()
    app = _apply(uid, "Acme", provenance="tracker", applied_day=1)
    _reply(uid, app, "rejection", domain="acme.test", day=11)

    timing = _block("acme", 1)["days_to_first_outcome"]
    assert timing["n"] == 1
    assert timing["median"] == 10.0
    assert timing["basis"] == "tracker_dated_only"


def test_a_median_needs_a_sample_too(f):
    """One timed application produced a "median" of 205 days against real data.
    That is not a median, it is one application wearing the word."""
    uid = f.make_user()
    app = _apply(uid, "Acme", provenance="tracker", applied_day=1)
    _reply(uid, app, "rejection", domain="acme.test", day=28)

    timing = _block("acme", rates.DEFAULT_MIN_SAMPLE)["days_to_first_outcome"]
    assert timing["n"] == 1
    assert timing["median"] is None
    assert timing["below_floor"] is True


def test_an_outcome_before_the_application_date_is_not_timed(f):
    """Three tracker rows have an outcome predating the date entered by hand.
    A negative wait is a data-entry artefact, not a fast employer."""
    uid = f.make_user()
    app = _apply(uid, "Acme", provenance="tracker", applied_day=20)
    _reply(uid, app, "rejection", domain="acme.test", day=2)

    assert _block("acme", 1)["days_to_first_outcome"]["n"] == 0


def test_applications_arriving_via_an_intermediary_are_excluded(f):
    """A course provider that replies to everyone is not a company that answers
    every applicant. Including them put a perfect response rate on a
    university."""
    uid = f.make_user()
    app = _apply(uid, "Some Programme", provenance="tracker", applied_day=1)
    _reply(uid, app, "offer", domain="university.test", day=2)

    assert _block("some programme", 1) is not None
    assert _block("some programme", 1, intermediaries=["university.test"]) is None


def test_an_employer_is_not_excluded_by_the_intermediary_rule(f):
    uid = f.make_user()
    app = _apply(uid, "Acme", provenance="tracker", applied_day=1)
    _reply(uid, app, "rejection", domain="acme.test", day=5)

    block = _block("acme", 1, intermediaries=["university.test"])
    assert block is not None
    assert block["replied"]["numerator"] == 1
