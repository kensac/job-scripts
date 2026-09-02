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
