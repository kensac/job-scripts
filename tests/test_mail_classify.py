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


def test_effort_is_accepted_by_the_configured_models():
    """A batch is submitted whole and fails whole. gpt-5.6-luna rejects
    "minimal" outright with a 400 - found by dry-running live calls before
    committing to a $10 batch, which is now the standing rule."""
    from api.ai import _EFFORTS_OPENAI

    assert mail_classify.CLASSIFY_EFFORT in _EFFORTS_OPENAI
    assert mail_classify.CLASSIFY_EFFORT != "minimal"


def test_max_tokens_leaves_room_for_the_schema():
    """Too small truncates JSON mid-string, which arrives as an unparsable
    line rather than an error - so it looks like a model failure, not a
    configuration one."""
    assert mail_classify.CLASSIFY_MAX_TOKENS >= 200
