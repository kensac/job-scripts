"""Sync live Gmail, import archives and probe credentials.

`sync_gmail` needs a live credential. `import_archive` reads a file without
one. `probe_credentials` has its own schedule because revocation is discovered
only when a token refresh is attempted. A stalled sync cannot detect its own
revoked credential. A worker fleet once remained idle for 28 minutes while
health checks stayed green.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from pathlib import Path
from typing import Any

from api import db, oauth
from api.mail import gmail
from api.mail import store as mail_store
from api.queue import enqueue
from core.mail.importer import ImportedMessage, read_archive
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)

# Rows read from an archive before flushing. The archive readers stream, so
# this bounds memory rather than the file: 38,685 messages at ~6KB of retained
# body is well over 200MB if accumulated, and these hosts already have
# "cannot allocate memory" as a named transient failure.
IMPORT_FLUSH = int(os.environ.get("JOBTRACKER_MAIL_IMPORT_FLUSH", "500"))

# Recent mail gets a quick overlapping search. Full reconciliation covers
# anything outside this window. Re-read messages dedupe on write.
SYNC_OVERLAP_DAYS = int(os.environ.get("JOBTRACKER_MAIL_SYNC_OVERLAP_DAYS", "2"))


def connected_user_ids() -> list[int]:
    """Users with a credential that is not known-dead.

    invalid_at IS NULL rather than a status column: "needs reconnect" is
    derived from the one fact that is recorded, not stored a second time where
    it could disagree with itself.
    """
    return [
        r["user_id"]
        for r in db.query(
            "SELECT user_id FROM user_oauth_tokens "
            "WHERE provider = %s AND invalid_at IS NULL ORDER BY user_id",
            (oauth.GOOGLE,),
        )
    ]


async def handle_probe_credentials(task_id: int, payload: dict[str, Any]) -> None:
    """Ask for a token and throw the answer away.

    A refresh is the only thing that can discover Google has revoked a grant,
    and the access token's own ~1h lifetime means this performs a real refresh
    roughly hourly and is a cached read otherwise. That is why there is no
    cadence constant here: the provider sets it.

    NeedsReconnect is allowed to propagate. The task fails, visibly, and
    health.detect() opens the alert. Catching it here would restore precisely
    the silence this handler exists to break.
    """
    users = connected_user_ids()
    set_progress(task_id, 0, len(users), "probing credentials")
    for i, user_id in enumerate(users, 1):
        oauth.get_access_token(user_id)
        set_progress(task_id, i, len(users), "probing credentials")


async def handle_sync_gmail(task_id: int, payload: dict[str, Any]) -> None:
    """Read recent mail and continue a full-mailbox reconciliation.

    Run in a thread so blocking Gmail and database calls do not stall the
    worker's event loop.
    """
    user_ids = [payload["user_id"]] if payload.get("user_id") else connected_user_ids()
    for user_id in user_ids:
        await asyncio.to_thread(_sync_one, task_id, user_id)
    if db.get_config("mail_classification_enabled"):
        enqueue("classify_mail", {}, dedupe_key=None)


def _sync_one(task_id: int, user_id: int) -> None:
    stored = 0
    limit = int(db.get_config("mail_sync_per_cycle"))
    pages_left = int(db.get_config("mail_recent_pages_per_cycle"))
    page_token: str | None = None
    after = _since(user_id)
    while pages_left and stored < limit:
        ids, next_token = gmail.list_message_page(user_id, page_token=page_token, after=after)
        pages_left -= 1
        messages = [gmail.fetch_message(user_id, message_id) for message_id in ids]
        stored += _store_unseen(user_id, messages, limit - stored)
        set_progress(task_id, stored, limit, "gmail sync")
        if not next_token:
            break
        page_token = next_token
    set_progress(task_id, stored, limit, "gmail sync")
    logger.info(f"gmail sync: stored {stored} new message(s) for user {user_id}")
    _reconcile_one(task_id, user_id)


def _store_unseen(user_id: int, messages: list[ImportedMessage], limit: int) -> int:
    """Store new messages from one bounded provider page."""
    if not messages:
        return 0
    ids = [message.provider_message_id for message in messages]
    seen = {
        row["provider_message_id"]
        for row in db.query(
            "SELECT provider_message_id FROM email_messages "
            "WHERE user_id = %s AND provider_message_id = ANY(%s)",
            (user_id, ids),
        )
    }
    new_messages = []
    for message in messages:
        if message.provider_message_id not in seen:
            new_messages.append(message)
            seen.add(message.provider_message_id)
    if new_messages:
        mail_store.store_messages(user_id, new_messages[:limit])
    return min(limit, len(new_messages))


def _reconcile_one(task_id: int, user_id: int) -> None:
    """Scan the mailbox in date ranges that resume without provider tokens."""
    db.execute(
        "INSERT INTO gmail_scan_cursors (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,),
    )
    ranges_left = int(db.get_config("mail_reconciliation_ranges_per_cycle"))
    messages_left = int(db.get_config("mail_reconciliation_messages_per_cycle"))
    checked = 0
    while messages_left:
        cursor = db.query_one(
            "SELECT pending_ids, ranges FROM gmail_scan_cursors WHERE user_id = %s",
            (user_id,),
        )
        assert cursor is not None
        pending = list(cursor["pending_ids"])
        ranges = list(cursor["ranges"])
        if pending:
            batch_ids = pending[:messages_left]
            messages = [gmail.fetch_message(user_id, message_id) for message_id in batch_ids]
            _store_unseen(user_id, messages, len(messages))
            checked += len(batch_ids)
            messages_left -= len(batch_ids)
            remaining = pending[len(batch_ids) :]
            advanced = db.execute_count(
                "UPDATE gmail_scan_cursors SET pending_ids = %s, updated_at = now() "
                "WHERE user_id = %s AND pending_ids = %s AND ranges = %s",
                (db.jsonb(remaining), user_id, db.jsonb(pending), db.jsonb(ranges)),
            )
            if advanced and not remaining and not ranges:
                _finish_reconciliation(user_id)
                break
            continue

        if not ranges:
            # A completed traversal starts again on the next scheduled sync.
            db.execute_count(
                "UPDATE gmail_scan_cursors SET ranges = '[[null, null]]'::jsonb, "
                "completed_at = NULL, updated_at = now() "
                "WHERE user_id = %s AND ranges = '[]'::jsonb AND pending_ids = '[]'::jsonb",
                (user_id,),
            )
            continue

        if not ranges_left:
            break

        start_epoch, end_epoch = ranges[-1]
        ids, next_token = gmail.list_message_page(
            user_id, after_epoch=start_epoch, before_epoch=end_epoch
        )
        ranges_left -= 1
        if next_token:
            midpoint = _split_epoch(start_epoch, end_epoch)
            if midpoint is None:
                raise gmail.GmailError(
                    "gmail has more than one page in a one-second range; "
                    "the scan cannot safely advance"
                )
            # The last range is read first. Check the newer half first.
            replacement = [*ranges[:-1], [start_epoch, midpoint], [midpoint, end_epoch]]
            db.execute_count(
                "UPDATE gmail_scan_cursors SET ranges = %s, updated_at = now() "
                "WHERE user_id = %s AND pending_ids = '[]'::jsonb AND ranges = %s",
                (db.jsonb(replacement), user_id, db.jsonb(ranges)),
            )
            continue

        replacement = ranges[:-1]
        claimed = db.execute_count(
            "UPDATE gmail_scan_cursors SET pending_ids = %s, ranges = %s, "
            "completed_at = NULL, updated_at = now() "
            "WHERE user_id = %s AND pending_ids = '[]'::jsonb AND ranges = %s",
            (db.jsonb(ids), db.jsonb(replacement), user_id, db.jsonb(ranges)),
        )
        if claimed and not ids and not replacement:
            _finish_reconciliation(user_id)
            break
    set_progress(task_id, checked, checked, "gmail reconciliation")
    logger.info("gmail reconciliation: checked %s message(s) for user %s", checked, user_id)


def _split_epoch(start_epoch: int | None, end_epoch: int | None) -> int | None:
    """Split one search range at a whole-second boundary."""
    if start_epoch is None and end_epoch is None:
        return int(datetime.datetime.now(datetime.UTC).timestamp())
    if start_epoch is None:
        assert end_epoch is not None
        return end_epoch - max(1, abs(end_epoch))
    if end_epoch is None:
        return start_epoch + max(1, abs(start_epoch))
    if end_epoch - start_epoch <= 1:
        return None
    return (start_epoch + end_epoch) // 2


def _finish_reconciliation(user_id: int) -> None:
    db.execute(
        "UPDATE gmail_scan_cursors SET completed_at = now(), updated_at = now() "
        "WHERE user_id = %s AND pending_ids = '[]'::jsonb AND ranges = '[]'::jsonb",
        (user_id,),
    )


def _since(user_id: int) -> str | None:
    """Return the recent-search date; the full sweep covers earlier mail."""
    row = db.query_one(
        "SELECT MAX(sent_at) AS newest FROM email_messages WHERE user_id = %s", (user_id,)
    )
    newest = row["newest"] if row else None
    if newest is None:
        return None
    # Normalised to UTC before the date is taken. psycopg returns a timestamptz
    # in the SESSION's timezone, and these containers run TZ=America/New_York
    # against a UTC database - so formatting it directly yields the previous
    # day for anything before 04:00Z, and the window silently starts a day
    # early. Harmless here because the window only ever over-reads, but the
    # same mistake elsewhere shifted every time window in this product by four
    # hours.
    newest = newest.astimezone(datetime.UTC)
    return (newest - datetime.timedelta(days=SYNC_OVERLAP_DAYS)).strftime("%Y/%m/%d")


async def handle_import_archive(task_id: int, payload: dict[str, Any]) -> None:
    """One-off import of a Takeout mbox or an Outlook .olm.

    Path comes from the payload rather than being discovered: pointing this at
    a directory it scans is how it eventually reads something it should not.
    These archives are the owner's entire mailbox.

    Threaded for the same reason as the sync, and more so - the Takeout mbox
    is 4.24GB and each .olm is ~4.4GB, so this occupies its thread for a long
    time and must not occupy the event loop with it.
    """
    await asyncio.to_thread(_import_one, task_id, payload)
    # Backfill classification is a different model and a different cost
    # profile from the ongoing trickle, so it is flagged rather than inferred.
    if db.get_config("mail_classification_enabled"):
        enqueue("classify_mail", {"backfill": True}, dedupe_key=None)


def _import_one(task_id: int, payload: dict[str, Any]) -> None:
    user_id = int(payload["user_id"])
    path = Path(payload["path"])
    if not path.is_file():
        raise FileNotFoundError(f"archive not found: {path}")

    stored = 0
    pending: list[Any] = []
    for message in read_archive(path):
        pending.append(message)
        if len(pending) >= IMPORT_FLUSH:
            stored += mail_store.store_messages(user_id, pending)
            pending = []
            set_progress(task_id, stored, 0, f"importing {path.name}")
    if pending:
        stored += mail_store.store_messages(user_id, pending)
    set_progress(task_id, stored, stored, f"imported {path.name}")
    logger.info(f"archive import: {stored} message(s) from {path.name} for user {user_id}")
