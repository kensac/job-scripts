"""Sync, import, and the credential probe.

The probe is the one worth reading carefully. Dead-credential detection is
discovery-on-use, so it exists as a separate scheduled task specifically so a
sync that stops running does not also stop noticing that it cannot run.
"""

from __future__ import annotations

import datetime

import pytest

from api import db, oauth
from api.mail import store as mail_store
from core.mail.importer import ImportedMessage
from tasks.mail import sync as mail_sync
from tests import mail_log


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
    monkeypatch.setattr(mail_sync, "set_progress", lambda *a, **k: None)
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
    monkeypatch.setattr(mail_sync, "set_progress", lambda *a, **k: None)
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
    monkeypatch.setattr(mail_sync, "set_progress", lambda *a, **k: None)
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

    def list_message_page(user_id, *, page_token=None, after=None):
        calls["listed"].append(after)
        return list(by_id), None

    def fetch_message(user_id, message_id):
        calls["fetched"].append(message_id)
        return by_id[message_id]

    monkeypatch.setattr(mail_sync.gmail, "list_message_page", list_message_page)
    monkeypatch.setattr(mail_sync.gmail, "fetch_message", fetch_message)
    monkeypatch.setattr(mail_sync, "_reconcile_one", lambda *_args: None)
    monkeypatch.setattr(mail_sync, "set_progress", lambda *a, **k: None)
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
    mail_log.execute(
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
async def test_sync_does_not_queue_classification_when_switched_off(monkeypatch, f):
    uid = f.make_user()
    _connect(uid)
    _gmail_stub(monkeypatch, [])
    queued: list[str] = []
    monkeypatch.setattr(mail_sync, "enqueue", lambda kind, *_args, **_kwargs: queued.append(kind))
    db.execute("UPDATE app_config SET value = 'false' WHERE key = 'mail_classification_enabled'")

    await mail_sync.handle_sync_gmail(1, {"user_id": uid})

    assert "classify_mail" not in queued


@pytest.mark.asyncio
async def test_sync_lets_needsreconnect_propagate(monkeypatch, f):
    """The probe is not the only path that can discover a dead grant, and the
    sync must not be the one that swallows it. A no-touch system that silently
    stops touching is the worst outcome."""
    uid = f.make_user()
    _connect(uid)

    def boom(user_id, message_id=None, **kw):
        raise oauth.NeedsReconnect("grant revoked")

    monkeypatch.setattr(mail_sync.gmail, "list_message_page", boom)
    monkeypatch.setattr(mail_sync, "set_progress", lambda *a, **k: None)
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

    monkeypatch.setattr(mail_sync.gmail, "list_message_page", boom)
    monkeypatch.setattr(mail_sync, "set_progress", lambda *a, **k: None)
    monkeypatch.setattr(mail_sync, "enqueue", lambda *a, **k: None)
    with pytest.raises(oauth.ProviderError):
        await mail_sync.handle_sync_gmail(1, {"user_id": uid})

    row = db.query_one("SELECT invalid_at FROM user_oauth_tokens WHERE user_id = %s", (uid,))
    assert row["invalid_at"] is None
    assert uid in mail_sync.connected_user_ids(), "the user must still be synced next cycle"


@pytest.mark.asyncio
async def test_a_sync_that_stops_short_resumes_rather_than_skipping(set_config, monkeypatch, f):
    """The cursor is the absence of a row, so a capped sweep leaves the rest
    for next time instead of advancing past it. A stored cursor would have
    skipped everything after the cap permanently."""
    uid = f.make_user()
    _connect(uid)
    set_config("mail_sync_per_cycle", 3)
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


@pytest.mark.asyncio
async def test_full_sweep_finds_mail_older_than_recent_window(monkeypatch, f, set_config):
    uid = f.make_user()
    _connect(uid)
    set_config("mail_reconciliation_ranges_per_cycle", 1)
    set_config("mail_reconciliation_messages_per_cycle", 3)
    newest = _message("<newest@x>")
    newest.sent_at = datetime.datetime(2026, 10, 10, tzinfo=datetime.UTC)
    old_one = _message("<old-one@x>")
    old_two = _message("<old-two@x>")
    mail_store.store_messages(uid, [newest])
    messages = {"newest": newest, "old-one": old_one, "old-two": old_two}
    page_calls: list[tuple[int | None, int | None]] = []
    fetched: list[str] = []

    def page(_user_id, *, page_token=None, after=None, after_epoch=None, before_epoch=None):
        if after is not None:
            return ["newest"], None
        page_calls.append((after_epoch, before_epoch))
        if after_epoch is None and before_epoch is None:
            return ["newest"], "more"
        if before_epoch == 0:
            return [], None
        if before_epoch is None:
            return [], None
        if after_epoch is None:
            return ["newest"], "more"
        if after_epoch == 0:
            return ["newest", "old-one", "old-two"], None
        return [], None

    def fetch(_user_id, message_id):
        fetched.append(message_id)
        return messages[message_id]

    monkeypatch.setattr(mail_sync.gmail, "list_message_page", page)
    monkeypatch.setattr(mail_sync.gmail, "fetch_message", fetch)
    monkeypatch.setattr(mail_sync, "set_progress", lambda *_args, **_kw: None)
    monkeypatch.setattr(mail_sync, "enqueue", lambda *_args, **_kw: None)

    cursor = None
    for _ in range(5):
        await mail_sync.handle_sync_gmail(1, {"user_id": uid})
        cursor = db.query_one(
            "SELECT completed_at FROM gmail_scan_cursors WHERE user_id = %s", (uid,)
        )
        if cursor and cursor["completed_at"] is not None:
            break
    assert len(page_calls) >= 4
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 3
    )
    assert cursor and cursor["completed_at"] is not None

    await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    assert page_calls[-1] == (None, None)
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 3
    )
    assert fetched.count("old-one") == 1


@pytest.mark.asyncio
async def test_reconciliation_keeps_unfinished_page_after_fetch_failure(monkeypatch, f, set_config):
    uid = f.make_user()
    _connect(uid)
    set_config("mail_reconciliation_ranges_per_cycle", 1)
    set_config("mail_reconciliation_messages_per_cycle", 2)
    newest = _message("<newest@x>")
    newest.sent_at = datetime.datetime(2026, 10, 10, tzinfo=datetime.UTC)
    mail_store.store_messages(uid, [newest])
    older = _message("<older@x>")
    attempts = 0
    pages = 0

    def page(_user_id, *, page_token=None, after=None, after_epoch=None, before_epoch=None):
        if after is not None:
            return [], None
        nonlocal pages
        pages += 1
        return ["older"], None

    def fetch(_user_id, _message_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise mail_sync.gmail.GmailError("temporary failure")
        return older

    monkeypatch.setattr(mail_sync.gmail, "list_message_page", page)
    monkeypatch.setattr(mail_sync.gmail, "fetch_message", fetch)
    monkeypatch.setattr(mail_sync, "set_progress", lambda *_args, **_kw: None)
    monkeypatch.setattr(mail_sync, "enqueue", lambda *_args, **_kw: None)

    with pytest.raises(mail_sync.gmail.GmailError):
        await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    pending = db.query_one("SELECT pending_ids FROM gmail_scan_cursors WHERE user_id = %s", (uid,))
    assert pending and pending["pending_ids"] == ["older"]

    await mail_sync.handle_sync_gmail(1, {"user_id": uid})
    assert pages == 1
    assert attempts == 2
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 2
    )


def test_reconciliation_search_ranges_overlap_exact_second_boundaries(monkeypatch):
    params: list[dict] = []
    monkeypatch.setattr(mail_sync.gmail.oauth, "get_access_token", lambda _user_id: "token")

    def get(_path, _token, **query):
        params.append(query)
        return {"messages": []}

    monkeypatch.setattr(mail_sync.gmail, "_get", get)
    mail_sync.gmail.list_message_page(1, after_epoch=10, before_epoch=11)
    assert params[0]["q"] == "after:9 before:11"
    assert mail_sync._split_epoch(10, 11) is None


@pytest.mark.asyncio
async def test_reconciliation_refuses_unsplittable_provider_page(monkeypatch, f, set_config):
    uid = f.make_user()
    _connect(uid)
    set_config("mail_reconciliation_ranges_per_cycle", 1)
    db.execute(
        "INSERT INTO gmail_scan_cursors (user_id, ranges) VALUES (%s, %s)",
        (uid, db.jsonb([[10, 11]])),
    )
    monkeypatch.setattr(mail_sync.gmail, "list_message_page", lambda *_args, **_kw: (["a"], "more"))
    with pytest.raises(mail_sync.gmail.GmailError, match="cannot safely advance"):
        mail_sync._reconcile_one(1, uid)
    cursor = db.query_one(
        "SELECT ranges, completed_at FROM gmail_scan_cursors WHERE user_id = %s", (uid,)
    )
    assert cursor == {"ranges": [[10, 11]], "completed_at": None}


@pytest.mark.asyncio
async def test_reconciliation_keeps_unfetched_ids_after_cycle_budget(monkeypatch, f, set_config):
    uid = f.make_user()
    _connect(uid)
    set_config("mail_reconciliation_ranges_per_cycle", 1)
    set_config("mail_reconciliation_messages_per_cycle", 1)
    db.execute(
        "INSERT INTO gmail_scan_cursors (user_id, ranges) VALUES (%s, %s)",
        (uid, db.jsonb([[0, 20]])),
    )
    pages = 0

    def page(_user_id, **_kw):
        nonlocal pages
        pages += 1
        return ["a", "b"], None

    monkeypatch.setattr(mail_sync.gmail, "list_message_page", page)
    monkeypatch.setattr(
        mail_sync.gmail,
        "fetch_message",
        lambda _user_id, message_id: _message(f"<{message_id}@x>"),
    )
    monkeypatch.setattr(mail_sync, "set_progress", lambda *_args, **_kw: None)
    mail_sync._reconcile_one(1, uid)
    cursor = db.query_one(
        "SELECT pending_ids, completed_at FROM gmail_scan_cursors WHERE user_id = %s", (uid,)
    )
    assert cursor == {"pending_ids": ["b"], "completed_at": None}
    mail_sync._reconcile_one(1, uid)
    assert pages == 1
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 2
    )


def test_new_mail_in_completed_range_is_seen_on_next_sweep(monkeypatch, f, set_config):
    uid = f.make_user()
    set_config("mail_reconciliation_ranges_per_cycle", 1)
    set_config("mail_reconciliation_messages_per_cycle", 3)
    db.execute(
        "INSERT INTO gmail_scan_cursors (user_id, ranges) VALUES (%s, %s)",
        (uid, db.jsonb([[0, 20]])),
    )
    at = {"older": 5, "recent": 15}

    def page(_user_id, *, after_epoch=None, before_epoch=None):
        matches = [
            name
            for name, epoch in at.items()
            if (after_epoch is None or epoch >= after_epoch)
            and (before_epoch is None or epoch < before_epoch)
        ]
        return matches[:1], "more" if len(matches) > 1 else None

    monkeypatch.setattr(mail_sync.gmail, "list_message_page", page)
    monkeypatch.setattr(
        mail_sync.gmail,
        "fetch_message",
        lambda _user_id, message_id: _message(f"<{message_id}@x>"),
    )
    monkeypatch.setattr(mail_sync, "set_progress", lambda *_args, **_kw: None)
    mail_sync._reconcile_one(1, uid)  # Split [0,20].
    mail_sync._reconcile_one(1, uid)  # Finish [10,20].
    at["arrived-late"] = 16
    mail_sync._reconcile_one(1, uid)  # Finish [0,10].
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 2
    )

    # The next full traversal repeats completed ranges and finds the new ID.
    db.execute(
        "UPDATE gmail_scan_cursors SET ranges = %s, completed_at = NULL WHERE user_id = %s",
        (db.jsonb([[0, 20]]), uid),
    )
    set_config("mail_reconciliation_ranges_per_cycle", 10)
    for _ in range(5):
        mail_sync._reconcile_one(1, uid)
    assert (
        db.query_one("SELECT count(*) AS c FROM email_messages WHERE user_id = %s", (uid,))["c"]
        == 3
    )
