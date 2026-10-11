"""Classification guards against inventing what an email did not say.

A fabricated deadline or rejection is unfalsifiable downstream: nothing later
in the pipeline can distinguish it from a real field.
"""

from __future__ import annotations

import datetime

import pytest

from api import db
from core import shapes
from tests.mail_classify_support import _events, _Res, _run, _store


@pytest.mark.asyncio
async def test_writes_an_event(monkeypatch, f):
    _uid, mid = _store(f)
    await _run(
        monkeypatch,
        {},
        {
            str(mid): _Res(
                '{"kind":"rejection","company":"Acme","role_title":"Engineer",'
                '"deadline":null,"deadline_is_explicit":false,"confidence":"high"}'
            )
        },
    )
    events = _events(mid)
    assert len(events) == 1
    assert events[0]["kind"] == "rejection"
    assert events[0]["detail"]["company"] == "Acme"
    assert events[0]["model"] == shapes.ONGOING_MODEL


@pytest.mark.asyncio
async def test_an_absent_deadline_stays_absent(monkeypatch, f):
    """Null means the email did not say. It must not become a date."""
    _uid, mid = _store(f)
    await _run(
        monkeypatch,
        {},
        {
            str(mid): _Res(
                '{"kind":"assessment_invite","company":null,"role_title":null,'
                '"deadline":null,"deadline_is_explicit":false,"confidence":"low"}'
            )
        },
    )
    events = _events(mid)
    assert events[0]["deadline_at"] is None
    assert events[0]["deadline_inferred"] is False


@pytest.mark.asyncio
async def test_an_implied_deadline_is_flagged_as_inferred(monkeypatch, f):
    """A date the model derived rather than read must be marked, because
    nothing may auto-fail on a guess."""
    _uid, mid = _store(f)
    await _run(
        monkeypatch,
        {},
        {
            str(mid): _Res(
                '{"kind":"assessment_invite","company":"Acme","role_title":null,'
                '"deadline":"2026-09-08","deadline_is_explicit":false,"confidence":"medium"}'
            )
        },
    )
    events = _events(mid)
    assert events[0]["deadline_at"] is not None
    assert events[0]["deadline_inferred"] is True


@pytest.mark.asyncio
async def test_unparsable_output_writes_nothing(monkeypatch, f):
    """A half-parsed result must not leave a half-written row that later looks
    like a completed classification - the bug shape that put partially
    populated comp on jobs and marked them extracted."""
    _uid, mid = _store(f)
    await _run(monkeypatch, {}, {str(mid): _Res("not json at all")})
    assert _events(mid) == []


@pytest.mark.asyncio
async def test_an_errored_line_writes_nothing_and_is_retried(monkeypatch, f):
    """Leaving it unclassified is what makes the sweep idempotent: the next
    pass picks it up because it selects messages with no events."""
    _uid, mid = _store(f)
    await _run(monkeypatch, {}, {str(mid): _Res(None, error="upstream failure")})
    assert _events(mid) == []
    remaining = db.query_one(
        "SELECT COUNT(*) AS c FROM email_messages m "
        "WHERE NOT EXISTS (SELECT 1 FROM email_events e WHERE e.message_id = m.id)",
    )
    assert remaining is not None and remaining["c"] >= 1


@pytest.mark.asyncio
async def test_not_job_related_is_a_real_verdict(monkeypatch, f):
    """Most mail is not job mail, and recording that is what stops the next
    sweep reclassifying the whole mailbox again."""
    _uid, mid = _store(f, from_email="alerts@chase.com", subject="Statement")
    await _run(
        monkeypatch,
        {},
        {
            str(mid): _Res(
                '{"kind":"not_job_related","company":null,"role_title":null,'
                '"deadline":null,"deadline_is_explicit":false,"confidence":"high"}'
            )
        },
    )
    assert _events(mid)[0]["kind"] == "not_job_related"


@pytest.mark.asyncio
async def test_one_unwritable_row_does_not_discard_the_batch(monkeypatch, f):
    """The batch is already paid for. Losing every other classification to one
    bad row is the expensive way to be strict - and the skipped row is picked
    up next sweep anyway, because it still has no event."""
    ids = []
    for i in range(3):
        _uid, mid = _store(f, mid=f"<bad{i}@x>")
        ids.append(mid)

    good = (
        '{"kind":"rejection","company":"Acme","role_title":null,'
        '"deadline":null,"deadline_is_explicit":false,"confidence":"high"}'
    )
    results = {str(m): _Res(good) for m in ids}
    # A key that is not an integer: int(key) raises inside the write.
    results["not-a-message-id"] = _Res(good)

    await _run(monkeypatch, {}, results)
    written = db.query_one(
        "SELECT count(*) AS c FROM email_events WHERE message_id = ANY(%s)", (ids,)
    )
    assert written is not None and written["c"] == 3


@pytest.mark.asyncio
async def test_a_scheduled_interview_lands_in_occurred_at_not_deadline_at(monkeypatch, f):
    """An interview time is not a deadline. Every one of these used to go into
    deadline_at with its clock stripped, while occurred_at - which
    mail_pipeline already selects - had never been written at all."""
    _uid, mid = _store(f, mid="<appt@x>")
    result = (
        '{"kind":"interview_scheduled","company":"Acme","role_title":null,'
        '"deadline":"Tue Dec 10, 2024 at 4:00PM (EST)",'
        '"deadline_is_explicit":true,"confidence":"high"}'
    )
    await _run(monkeypatch, {}, {str(mid): _Res(result)})

    row = db.query_one(
        "SELECT occurred_at, deadline_at, detail FROM email_events WHERE message_id = %s",
        (mid,),
    )
    assert row is not None
    assert row["deadline_at"] is None
    assert row["occurred_at"] == datetime.datetime(2024, 12, 10, 21, 0, tzinfo=datetime.UTC)
    assert row["detail"]["when_precision"] == "instant"


@pytest.mark.asyncio
async def test_an_unparsed_date_keeps_the_raw_string(monkeypatch, f):
    """Without this the failures cannot be studied. The parser was rewritten
    against a corpus of failures nobody had kept, which is why the shapes it
    was said to be dropping turned out to be ones it already handled."""
    _uid, mid = _store(f, mid="<raw@x>")
    result = (
        '{"kind":"interview_scheduled","company":"Acme","role_title":null,'
        '"deadline":"Tuesday at 2:00 PM",'
        '"deadline_is_explicit":true,"confidence":"high"}'
    )
    await _run(monkeypatch, {}, {str(mid): _Res(result)})

    row = db.query_one(
        "SELECT occurred_at, deadline_at, detail FROM email_events WHERE message_id = %s",
        (mid,),
    )
    assert row is not None
    # A dateless time is not resolvable and a wrong interview time is worse
    # than a missing one, so nothing is stored but the string itself.
    assert row["occurred_at"] is None
    assert row["deadline_at"] is None
    assert row["detail"]["when_raw"] == "Tuesday at 2:00 PM"
    assert row["detail"]["when_precision"] is None


@pytest.mark.asyncio
async def test_a_deadline_still_lands_in_deadline_at(monkeypatch, f):
    """The appointment split must not swallow ordinary deadlines."""
    _uid, mid = _store(f, mid="<dl@x>")
    result = (
        '{"kind":"assessment_invite","company":"Acme","role_title":null,'
        '"deadline":"May 30, 2024, 5:00 PM Pacific Time",'
        '"deadline_is_explicit":true,"confidence":"high"}'
    )
    await _run(monkeypatch, {}, {str(mid): _Res(result)})

    row = db.query_one(
        "SELECT occurred_at, deadline_at FROM email_events WHERE message_id = %s", (mid,)
    )
    assert row is not None
    assert row["occurred_at"] is None
    assert row["deadline_at"] == datetime.datetime(2024, 5, 31, 0, 0, tzinfo=datetime.UTC)


@pytest.mark.asyncio
async def test_mail_that_is_not_job_related_records_no_deadline(monkeypatch, f):
    """Two thirds of every deadline in the corpus - 1,409 of 2,128 - sat on
    mail the classifier itself had called not job related. Each one became an
    action item and fed the "quiet for 60+ days" signal from a newsletter.

    This applies the model's own answer to its own other answer rather than
    making a second judgement: it already said the message is not about a job,
    so whatever date it found is a marketing expiry, not a deadline."""
    _uid, mid = _store(f, mid="<mktg@x>")
    result = (
        '{"kind":"not_job_related","company":null,"role_title":null,'
        '"deadline":"March 1, 2026","deadline_is_explicit":true,"confidence":"high"}'
    )
    await _run(monkeypatch, {}, {str(mid): _Res(result)})

    row = db.query_one(
        "SELECT kind, deadline_at, occurred_at, detail FROM email_events WHERE message_id = %s",
        (mid,),
    )
    assert row is not None
    assert row["kind"] == "not_job_related"
    assert row["deadline_at"] is None
    assert row["occurred_at"] is None
    # The date is still recorded as raw text, so a wrongly-dropped one stays
    # auditable rather than vanishing.
    assert row["detail"]["when_raw"] == "March 1, 2026"
    assert row["detail"]["when_dropped_as_not_job_related"] is True


@pytest.mark.asyncio
async def test_a_job_related_deadline_is_still_recorded(monkeypatch, f):
    """The drop must be scoped to the one kind, not to deadlines generally."""
    _uid, mid = _store(f, mid="<real@x>")
    result = (
        '{"kind":"assessment_invite","company":"Acme","role_title":null,'
        '"deadline":"March 1, 2026","deadline_is_explicit":true,"confidence":"high"}'
    )
    await _run(monkeypatch, {}, {str(mid): _Res(result)})

    row = db.query_one("SELECT deadline_at, detail FROM email_events WHERE message_id = %s", (mid,))
    assert row is not None
    assert row["deadline_at"] == datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    assert row["detail"]["when_dropped_as_not_job_related"] is False
