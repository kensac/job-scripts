from __future__ import annotations

import datetime

import pytest

from api import db
from api.mail import store as mail_store
from core.mail.importer import ImportedMessage
from tasks.mail.classification import handler as mail_classify
from tests import mail_log
from tests.mail_classify_support import _events, _Res, _run, _store


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
    mail_log.execute(
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
    monkeypatch.setattr(mail_classify, "set_progress", lambda *a, **k: None)
    await mail_classify.handle_classify_mail(1, {"message_ids": [mid]})

    events = _events(mid)
    assert len(events) == 2, "append-only: the wrong event is superseded, not deleted"
    assert events[-1]["kind"] == "not_job_related"
    assert events[-1]["model"] == "rule:self_sent", "no model was paid for a header fact"
    assert events[-1]["detail"]["reason"] == "self_sent"
    assert called is False, "no AI call is made for a message the header already settles"


@pytest.mark.asyncio
async def test_a_sweep_does_not_reselect_what_another_sweep_is_already_paying_for(monkeypatch, f):
    """'No events yet' stays true for the whole time a message sits in the
    provider's queue. So an hourly sweep selected the same messages again and
    submitted them again: three tasks an hour apart, each carrying an identical
    1,156 requests, all in flight at once. The cost is not queue depth - it is
    paying repeatedly for one answer."""
    from api.worker import AwaitingBatch

    for i in range(3):
        _store(f, mid=f"<inflight{i}@x>")

    first_claim: list[int] = []

    async def capture(task_id, shape, specs):

        first_claim.extend(int(s.custom_id) for s in specs)
        raise AwaitingBatch(["batch_stuck"])

    monkeypatch.setattr(mail_classify, "run_batched", capture)
    monkeypatch.setattr(mail_classify, "set_progress", lambda *a, **k: None)

    first = f.make_task("classify_mail", {})
    with pytest.raises(AwaitingBatch):
        await mail_classify.handle_classify_mail(first, {})
    assert len(first_claim) == 3
    db.execute("UPDATE tasks SET status = 'awaiting_batch' WHERE id = %s", (first,))

    second_claim: list[int] = []

    async def second_capture(task_id, shape, specs):
        from core.routing import resolve

        second_claim.extend(int(s.custom_id) for s in specs)
        return [], resolve(shape)

    monkeypatch.setattr(mail_classify, "run_batched", second_capture)
    await mail_classify.handle_classify_mail(f.make_task("classify_mail", {}), {})
    assert second_claim == [], "the first task is already paying for these"
