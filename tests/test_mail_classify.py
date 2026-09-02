"""Classification, and the guards against inventing what an email did not say.

The failure this must not have: a deadline that was never stated becoming a
date, or silence becoming a rejection. Those are unfalsifiable downstream -
nothing later in the pipeline can tell a fabricated field from a real one.
"""

from __future__ import annotations

import datetime

import pytest

from api import db, mail_store
from api.tasks import HANDLERS, mail_classify
from core import pricing
from core.mail_import import ImportedMessage


class _Res:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.usage = {}
        self.batch_id = "b1"


def _store(f, **kw) -> tuple[int, int]:
    uid = f.make_user()
    mail_store.store_messages(
        uid,
        [
            ImportedMessage(
                provider_message_id=kw.get("mid", "<c1@x>"),
                source="gmail",
                from_email=kw.get("from_email", "no-reply@greenhouse.io"),
                subject=kw.get("subject", "Your application"),
                sent_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
                body_text=kw.get("body", "Thanks for applying."),
            )
        ],
    )
    row = db.query_one("SELECT id FROM email_messages WHERE user_id = %s", (uid,))
    assert row is not None
    return uid, row["id"]


def _events(message_id: int):
    return db.query("SELECT * FROM email_events WHERE message_id = %s ORDER BY id", (message_id,))


async def _run(monkeypatch, payload, results):
    async def fake(task_id, shape, specs):
        from core.routing import resolve

        return results, resolve(shape)

    monkeypatch.setattr(mail_classify, "run_batched", fake)
    monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
    await mail_classify.handle_classify_mail(1, payload)


def test_both_models_are_priced():
    """If a model is missing from the price table its spend books as NULL and
    the classification run is invisible to /admin/spend - a silent hole in
    exactly the surface built to catch silent holes."""
    assert pricing.rates_for(mail_classify.BACKFILL_MODEL) is not None
    assert pricing.rates_for(mail_classify.ONGOING_MODEL) is not None


def test_handler_is_registered():
    assert HANDLERS["classify_mail"] is mail_classify.handle_classify_mail


def test_backfill_and_ongoing_use_different_models():
    """The one-time sweep and the daily trickle are priced differently enough
    to be different models; inheriting one for both loses that on purpose."""
    assert mail_classify.BACKFILL_MODEL != mail_classify.ONGOING_MODEL


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
    assert events[0]["model"] == mail_classify.ONGOING_MODEL


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


# Probed against the live APIs. Each model REJECTS the other's cheapest value,
# so the intersection is only {low, medium, high}.
_PROBED_ACCEPTS = {
    "gpt-5-mini": {"minimal", "low", "medium", "high"},
    "gpt-5.6-luna": {"none", "low", "medium", "high", "xhigh", "max"},
}


def test_each_model_gets_an_effort_it_actually_accepts():
    """The bug this replaces: the original test asserted the effort was in
    ai._EFFORTS_OPENAI, a UNION across model generations. "none" is in that
    union because the 5.6 family accepts it, so the test passed while
    gpt-5-mini rejected the value on every call - 400, and a batch submits
    whole and fails whole.

    Backfill worked and ongoing did not, which is the worst shape for
    noticing: the path exercised by hand was fine and the scheduled one was
    dead. Validating against a union is what hid it, so this validates
    against the models actually configured.
    """
    for model in (mail_classify.BACKFILL_MODEL, mail_classify.ONGOING_MODEL):
        accepts = _PROBED_ACCEPTS.get(model)
        assert accepts is not None, f"{model} configured but never probed"
        assert mail_classify.effort_for(model) in accepts


def test_an_unknown_model_gets_a_value_both_generations_accept():
    """A rejected parameter costs the whole batch, not one call, so the
    fallback has to be in the intersection rather than a guess."""
    effort = mail_classify.effort_for("some-model-that-ships-tomorrow")
    for accepts in _PROBED_ACCEPTS.values():
        assert effort in accepts


def test_the_two_models_do_not_share_an_effort_by_accident():
    """If they ever do, it should be because the intersection changed, not
    because someone collapsed the table back to one constant."""
    assert mail_classify.effort_for(mail_classify.BACKFILL_MODEL) == "none"
    assert mail_classify.effort_for(mail_classify.ONGOING_MODEL) == "minimal"


def test_max_tokens_leaves_room_for_the_schema():
    """Too small truncates JSON mid-string, which arrives as an unparsable
    line rather than an error - so it looks like a model failure, not a
    configuration one."""
    assert mail_classify.CLASSIFY_MAX_TOKENS >= 200


def test_a_backfill_may_ask_for_more_than_the_hourly_cap():
    """34,000 archived messages take ~28 hours of hourly cycles at the ongoing
    cap. A one-time sweep is a different job from a trickle."""
    assert mail_classify.MAX_CLASSIFY_PER_CYCLE > mail_classify.CLASSIFY_PER_CYCLE


@pytest.mark.asyncio
async def test_the_cap_is_clamped_not_trusted(monkeypatch, f):
    """An enqueuer asking for the whole mailbox would build a spec list far
    larger than a wave can carry, and that failure arrives as memory pressure
    on a worker rather than as a rejected parameter."""
    seen: dict = {}

    async def fake(task_id, shape, specs):
        from core.routing import resolve

        seen["count"] = len(specs)
        return {}, resolve(shape)

    for i in range(3):
        _store(f, mid=f"<cap{i}@x>")
    monkeypatch.setattr(mail_classify, "run_batched", fake)
    monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
    monkeypatch.setattr(mail_classify, "MAX_CLASSIFY_PER_CYCLE", 2)
    await mail_classify.handle_classify_mail(1, {"cap": 999999})
    assert seen["count"] <= 2


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


# --- the time of day, which used to be thrown away entirely --------------


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
async def test_reclassify_by_id_revisits_a_message_that_already_has_an_event(monkeypatch, f):
    """The default path skips anything already classified, which is what stops
    the classifier re-paying for the whole mailbox every sweep. Repairing a
    wrong event needs to reach past that, and does so with an explicit set
    rather than by loosening the default."""
    _uid, mid = _store(f, mid="<again@x>")
    first = (
        '{"kind":"offer","company":"Acme","role_title":null,'
        '"deadline":null,"deadline_is_explicit":false,"confidence":"high"}'
    )
    await _run(monkeypatch, {}, {str(mid): _Res(first)})

    # The default sweep now has nothing to do for this message.
    await _run(monkeypatch, {}, {str(mid): _Res(first)})
    assert len(_events(mid)) == 1

    corrected = (
        '{"kind":"not_job_related","company":null,"role_title":null,'
        '"deadline":null,"deadline_is_explicit":false,"confidence":"high"}'
    )
    await _run(monkeypatch, {"message_ids": [mid]}, {str(mid): _Res(corrected)})

    events = _events(mid)
    assert len(events) == 2, "the log is append-only; the old event is kept"
    # Latest per message wins, which is how the correction takes effect
    # without deleting or migrating anything.
    assert events[-1]["kind"] == "not_job_related"


@pytest.mark.asyncio
async def test_reclassify_refuses_a_list_over_the_cap(monkeypatch, f):
    """Truncating a repair silently leaves the rest wrong with nothing
    recording which half ran."""
    with pytest.raises(ValueError, match="over the cap"):
        await _run(monkeypatch, {"message_ids": list(range(1, 12)), "cap": 10}, {})


@pytest.mark.asyncio
async def test_reclassify_ignores_ids_that_do_not_exist(monkeypatch, f):
    """A stale id in a caller's list must not fail the whole repair."""
    _uid, mid = _store(f, mid="<ghost@x>")
    result = (
        '{"kind":"rejection","company":"Acme","role_title":null,'
        '"deadline":null,"deadline_is_explicit":false,"confidence":"high"}'
    )
    await _run(monkeypatch, {"message_ids": [mid, mid + 99_999]}, {str(mid): _Res(result)})
    assert len(_events(mid)) == 1


@pytest.mark.asyncio
async def test_the_sweep_skips_mail_the_candidate_sent(monkeypatch, f):
    """The instruction asks what "the SENDER is communicating", so when the
    sender IS the candidate every kind comes out backwards - his own reply
    saying he had started work and was enjoying it was recorded as an offer.
    942 messages are self-sent and 137 carried inbound events."""
    uid = f.make_user(email="me@example.test")
    mail_store.store_messages(
        uid,
        [
            ImportedMessage(
                provider_message_id="<sent@x>",
                source="gmail",
                from_email="Me <me@example.test>",
                subject="Re: Congratulations on the new role",
                sent_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
                body_text="I started work last week and am really enjoying it.",
            )
        ],
    )
    row = db.query_one("SELECT id FROM email_messages WHERE user_id = %s", (uid,))
    assert row is not None

    # A result IS supplied for this message. Without it the test passes whether
    # or not the predicate exists, because an empty result set writes nothing
    # either way - which is exactly the tautology this suite has been bitten by
    # before. With it, a sweep that selected the message would write an event.
    offer = (
        '{"kind":"offer","company":null,"role_title":null,'
        '"deadline":null,"deadline_is_explicit":false,"confidence":"high"}'
    )
    await _run(monkeypatch, {}, {str(row["id"]): _Res(offer)})
    assert _events(row["id"]) == [], "self-sent mail is not an inbound event"
    # The MESSAGE is kept: it is the best record of when he applied, which is a
    # different signal to be built deliberately.
    assert db.query_one("SELECT id FROM email_messages WHERE id = %s", (row["id"],)) is not None


@pytest.mark.asyncio
async def test_repairing_a_self_sent_message_corrects_it_without_a_model(monkeypatch, f):
    """The 137 already-written events need correcting, not just excluding from
    future sweeps. Direction is a header fact, so the correction is written
    directly rather than paid for."""
    uid = f.make_user(email="me2@example.test")
    mail_store.store_messages(
        uid,
        [
            ImportedMessage(
                provider_message_id="<sent2@x>",
                source="gmail",
                from_email="me2@example.test",
                subject="Re: offer",
                sent_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
                body_text="Thanks, I accepted!",
            )
        ],
    )
    row = db.query_one("SELECT id FROM email_messages WHERE user_id = %s", (uid,))
    assert row is not None
    mid = row["id"]
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, model) "
        "VALUES (%s, 'offer', 'high', 'gpt-5-mini')",
        (mid,),
    )

    called = False

    async def never(*a, **k):
        nonlocal called
        called = True
        return {}, None

    monkeypatch.setattr(mail_classify, "run_batched", never)
    monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
    await mail_classify.handle_classify_mail(1, {"message_ids": [mid]})

    events = _events(mid)
    assert len(events) == 2, "append-only: the wrong event is superseded, not deleted"
    assert events[-1]["kind"] == "not_job_related"
    assert events[-1]["model"] is None, "no model was paid for a header fact"
    assert events[-1]["detail"]["reason"] == "self_sent"
    assert called is False, "no AI call is made for a message the header already settles"


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
