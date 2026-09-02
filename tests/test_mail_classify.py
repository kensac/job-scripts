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
from core.mail_import import ImportedMessage
from core.pricing import PRICES_PER_MTOK


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
    async def fake(task_id, specs, model, effort, max_tokens, hook):
        return results

    monkeypatch.setattr(mail_classify, "submit_or_collect", fake)
    monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
    monkeypatch.setattr(mail_classify, "_batch_event_hook", lambda *a, **k: None)
    await mail_classify.handle_classify_mail(1, payload)


def test_both_models_are_priced():
    """If a model is missing from the price table its spend books as NULL and
    the classification run is invisible to /admin/spend - a silent hole in
    exactly the surface built to catch silent holes."""
    assert mail_classify.BACKFILL_MODEL in PRICES_PER_MTOK
    assert mail_classify.ONGOING_MODEL in PRICES_PER_MTOK


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

    async def fake(task_id, specs, model, effort, max_tokens, hook):
        seen["count"] = len(specs)
        return {}

    for i in range(3):
        _store(f, mid=f"<cap{i}@x>")
    monkeypatch.setattr(mail_classify, "submit_or_collect", fake)
    monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
    monkeypatch.setattr(mail_classify, "_batch_event_hook", lambda *a, **k: None)
    monkeypatch.setattr(mail_classify, "MAX_CLASSIFY_PER_CYCLE", 2)
    await mail_classify.handle_classify_mail(1, {"cap": 999999})
    assert seen["count"] <= 2


SENT = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)


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
        ("May 30, 2024, 5:00 PM Pacific Time", datetime.date(2024, 5, 30), False),
        # No year: resolved against the message and MARKED inferred.
        ("Jan. 15", datetime.date(2027, 1, 15), True),
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
    got, inferred = mail_classify.parse_deadline(raw, sent_at=SENT)
    assert (got.date() if got else None) == expected_date
    assert inferred is expected_inferred
    if got is not None:
        assert got.tzinfo is not None


def test_a_yearless_date_without_a_message_date_is_dropped():
    """Resolving it against TODAY would attach a deadline to an archived 2022
    email based on when the classifier happened to run."""
    assert mail_classify.parse_deadline("Jan. 15") == (None, False)


def test_a_yearless_date_rolls_forward_rather_than_backward():
    """ "Jan. 15" in a December email means the following January. Resolving to
    the message's own year would put the deadline before the email."""
    december = datetime.datetime(2026, 12, 20, tzinfo=datetime.UTC)
    got, inferred = mail_classify.parse_deadline("Jan. 15", sent_at=december)
    assert got is not None and got.date() == datetime.date(2027, 1, 15)
    assert inferred is True


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
