"""Sync, import, and the credential probe.

The probe is the one worth reading carefully. Dead-credential detection is
discovery-on-use, so it exists as a separate scheduled task specifically so a
sync that stops running does not also stop noticing that it cannot run.
"""

from __future__ import annotations

import datetime

import pytest

from api import db, mail_store, oauth
from api.tasks import mail_sync
from core.mail_import import ImportedMessage


def _connect(user_id: int, *, invalid: bool = False) -> None:
    db.execute(
        """
        INSERT INTO user_oauth_tokens (user_id, provider, refresh_token_enc, scopes, invalid_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id, provider) DO UPDATE SET invalid_at = EXCLUDED.invalid_at
        """,
        (
            user_id,
            oauth.GOOGLE,
            b"enc",
            ["https://www.googleapis.com/auth/gmail.readonly"],
            datetime.datetime.now(datetime.UTC) if invalid else None,
        ),
    )


def test_connected_users_excludes_dead_credentials(f):
    live, dead = f.make_user(), f.make_user()
    _connect(live)
    _connect(dead, invalid=True)
    ids = mail_sync.connected_user_ids()
    assert live in ids
    assert dead not in ids


@pytest.mark.asyncio
async def test_probe_lets_needsreconnect_propagate(monkeypatch, f):
    """The whole point. Catching it here would restore exactly the silence
    the probe exists to break - a no-touch system that quietly stops
    touching."""
    uid = f.make_user()
    _connect(uid)

    def boom(user_id, provider=oauth.GOOGLE):
        raise oauth.NeedsReconnect("grant revoked")

    monkeypatch.setattr(mail_sync.oauth, "get_access_token", boom)
    monkeypatch.setattr(mail_sync, "_set_progress", lambda *a, **k: None)
    with pytest.raises(oauth.NeedsReconnect):
        await mail_sync.handle_probe_credentials(1, {})


@pytest.mark.asyncio
async def test_probe_discards_the_token(monkeypatch, f):
    """It asks only so that a refusal is discovered. Nothing uses the answer."""
    uid = f.make_user()
    _connect(uid)
    calls = []
    monkeypatch.setattr(
        mail_sync.oauth, "get_access_token", lambda u, provider=None: calls.append(u) or "tok"
    )
    monkeypatch.setattr(mail_sync, "_set_progress", lambda *a, **k: None)
    await mail_sync.handle_probe_credentials(1, {})
    assert calls == [uid]


def test_sync_window_is_none_on_an_empty_mailbox(f):
    uid = f.make_user()
    assert mail_sync._since(uid) is None


def test_sync_window_overlaps_rather_than_advancing_to_the_newest(f):
    """A cursor at the newest message would permanently skip mail delivered
    late or out of order. The window reaches back so those still land, and
    the re-read messages dedupe on write."""
    uid = f.make_user()
    newest = datetime.datetime(2026, 9, 10, tzinfo=datetime.UTC)
    mail_store.store_messages(
        uid,
        [
            ImportedMessage(
                provider_message_id="<w1@x>", source="gmail", sent_at=newest, subject="hi"
            )
        ],
    )
    since = mail_sync._since(uid)
    assert since is not None
    expected = (newest - datetime.timedelta(days=mail_sync.SYNC_OVERLAP_DAYS)).strftime("%Y/%m/%d")
    # Exact, not approximate: psycopg returns timestamptz in the SESSION's
    # timezone and these containers run TZ=America/New_York against a UTC
    # database, so a window computed without normalising lands a day early.
    assert since == expected
    assert since < newest.strftime("%Y/%m/%d")


@pytest.mark.asyncio
async def test_import_rejects_a_missing_file(f):
    """Fail loudly rather than importing zero messages and reporting success -
    a silent no-op here looks identical to an empty archive."""
    uid = f.make_user()
    with pytest.raises(FileNotFoundError):
        await mail_sync.handle_import_archive(1, {"user_id": uid, "path": "/nope/absent.mbox"})


@pytest.mark.asyncio
async def test_import_reads_an_archive_and_queues_backfill(monkeypatch, tmp_path, f):
    uid = f.make_user()
    mbox = tmp_path / "a.mbox"
    mbox.write_text(
        "From b@x Mon Sep  1 10:00:00 2026\n"
        "From: no-reply@greenhouse.io\nSubject: Your application\n"
        "Message-ID: <imp1@x>\nDate: Mon, 1 Sep 2026 10:00:00 +0000\n"
        "Content-Type: text/plain\n\nThanks for applying.\n"
    )
    queued: list[tuple] = []
    monkeypatch.setattr(mail_sync, "enqueue", lambda k, p, dedupe_key=None: queued.append((k, p)))
    monkeypatch.setattr(mail_sync, "_set_progress", lambda *a, **k: None)
    await mail_sync.handle_import_archive(1, {"user_id": uid, "path": str(mbox)})

    row = db.query_one(
        "SELECT * FROM email_messages WHERE user_id = %s AND provider_message_id = %s",
        (uid, "<imp1@x>"),
    )
    assert row is not None and row["source"] == "takeout"
    # Backfill is a different model and cost profile from the ongoing trickle,
    # so it is flagged rather than inferred downstream.
    assert queued == [("classify_mail", {"backfill": True})]


# ---------------------------------------------------------------------------
# The ongoing path under the conditions it will actually meet
# ---------------------------------------------------------------------------


def _gmail_stub(monkeypatch, messages: list[ImportedMessage]) -> dict[str, list]:
    """A mailbox that answers list/fetch, recording what was asked for."""
    calls: dict[str, list] = {"listed": [], "fetched": []}
    by_id = {m.provider_message_id: m for m in messages}

    def list_message_ids(user_id, *, after=None):
        calls["listed"].append(after)
        return iter(list(by_id))

    def fetch_message(user_id, message_id):
        calls["fetched"].append(message_id)
        return by_id[message_id]

    monkeypatch.setattr(mail_sync.gmail, "list_message_ids", list_message_ids)
    monkeypatch.setattr(mail_sync.gmail, "fetch_message", fetch_message)
    monkeypatch.setattr(mail_sync, "_set_progress", lambda *a, **k: None)
    monkeypatch.setattr(mail_sync, "enqueue", lambda *a, **k: None)
    return calls


def _message(mid: str, *, subject: str = "hi") -> ImportedMessage:
    return ImportedMessage(
        provider_message_id=mid,
        source="gmail",
        sent_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
        subject=subject,
    )


@pytest.mark.asyncio
async def test_a_second_sync_re_fetches_but_stores_nothing(monkeypatch, f):
    """The archives overlap live Gmail heavily, so the first live sync re-sees
    thousands of already-imported messages.

    Note what this does and does not save. `seen` is checked AFTER the fetch,
    because the list returns Gmail's own ids while dedupe is on the RFC
    Message-ID - so a re-seen message still costs one quota unit and is only
    saved a write. That is bounded by the two-day window rather than by the
    mailbox, which is what makes it affordable; it is not free.
    """
    uid = f.make_user()
    _connect(uid)
    messages = [_message(f"<dup{i}@x>") for i in range(5)]
    calls = _gmail_stub(monkeypatch, messages)

    await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    first = db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
    assert first == 5
    assert len(calls["fetched"]) == 5

    fetched_before = len(calls["fetched"])
    await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    second = db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))[
        "c"
    ]
    assert second == 5, "a re-sync must not duplicate rows"
    # Fetched again, and that is the documented cost. Asserted so that the day
    # someone moves the `seen` check ahead of the fetch, this test notices the
    # improvement instead of having silently claimed it was already true.
    assert len(calls["fetched"]) == fetched_before + 5


@pytest.mark.asyncio
async def test_resync_does_not_requeue_already_classified_mail(monkeypatch, f):
    """Dedupe on write is only half of it. Re-seeing a message must also not
    put it back in front of the classifier, which is where the cost is."""
    uid = f.make_user()
    _connect(uid)
    _gmail_stub(monkeypatch, [_message("<seen@x>")])
    await mail_sync.handle_sync_gmail(1, {"user_id": uid})

    message_id = db.query_one("SELECT id FROM email_messages WHERE user_id = %s", (uid,))["id"]
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, model) VALUES (%s,%s,%s,%s)",
        (message_id, "rejection", 0.9, "test"),
    )

    await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    unclassified = db.query_one(
        """
        SELECT count(*) AS c FROM email_messages m
        WHERE m.user_id = %s
          AND NOT EXISTS (SELECT 1 FROM email_events e WHERE e.message_id = m.id)
        """,
        (uid,),
    )["c"]
    assert unclassified == 0, "a re-synced message must not become classifiable again"


@pytest.mark.asyncio
async def test_sync_lets_needsreconnect_propagate(monkeypatch, f):
    """The probe is not the only path that can discover a dead grant, and the
    sync must not be the one that swallows it. A no-touch system that silently
    stops touching is the worst outcome."""
    uid = f.make_user()
    _connect(uid)

    def boom(user_id, message_id=None, **kw):
        raise oauth.NeedsReconnect("grant revoked")

    monkeypatch.setattr(mail_sync.gmail, "list_message_ids", boom)
    monkeypatch.setattr(mail_sync, "_set_progress", lambda *a, **k: None)
    monkeypatch.setattr(mail_sync, "enqueue", lambda *a, **k: None)
    with pytest.raises(oauth.NeedsReconnect):
        await mail_sync.handle_sync_gmail(1, {"user_id": uid})


@pytest.mark.asyncio
async def test_sync_lets_providererror_propagate_without_killing_the_grant(monkeypatch, f):
    """Retryable is not dead. A transient provider failure must fail the task
    and leave the credential alone, or a bad afternoon at Google disconnects
    the mailbox."""
    uid = f.make_user()
    _connect(uid)

    def boom(user_id, **kw):
        raise oauth.ProviderError("503 backend_error")

    monkeypatch.setattr(mail_sync.gmail, "list_message_ids", boom)
    monkeypatch.setattr(mail_sync, "_set_progress", lambda *a, **k: None)
    monkeypatch.setattr(mail_sync, "enqueue", lambda *a, **k: None)
    with pytest.raises(oauth.ProviderError):
        await mail_sync.handle_sync_gmail(1, {"user_id": uid})

    row = db.query_one("SELECT invalid_at FROM user_oauth_tokens WHERE user_id = %s", (uid,))
    assert row["invalid_at"] is None
    assert uid in mail_sync.connected_user_ids(), "the user must still be synced next cycle"


@pytest.mark.asyncio
async def test_a_sync_that_stops_short_resumes_rather_than_skipping(monkeypatch, f):
    """The cursor is the absence of a row, so a capped sweep leaves the rest
    for next time instead of advancing past it. A stored cursor would have
    skipped everything after the cap permanently."""
    uid = f.make_user()
    _connect(uid)
    monkeypatch.setattr(mail_sync, "SYNC_BATCH", 3)
    _gmail_stub(monkeypatch, [_message(f"<m{i}@x>") for i in range(7)])

    await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 3
    )

    await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 6
    ), "the next pass must pick up where the last one stopped"
