"""Getting mail into the database: live Gmail, and one-off archive imports.

Three handlers, deliberately separate because they fail differently:

- sync_gmail        recurring, small, needs a live credential
- import_archive    one-off, enormous, needs a file and no credential
- probe_credentials recurring, tiny, exists ONLY to notice a dead token

The last one is not a convenience. Dead-credential detection is
discovery-on-use: `invalid_at` is written when a refresh is refused, and
nothing refuses a refresh unless something asks for a token. If that only
happened inside the sync, then a sync that stops running also stops noticing
that it cannot run - the alarm wired to the thing it is alarming about, which
is exactly how a worker fleet sat dead for 28 minutes behind green
healthchecks. So the probe has its own kind and its own schedule.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from pathlib import Path
from typing import Any

from api import db, gmail, mail_store, oauth
from api.tasks.runtime import _set_progress, enqueue
from core.mail_import import read_archive

logger = logging.getLogger("jobtracker_worker")

# Messages fetched per sync. Gmail's own page size is 500 and its per-message
# get is one quota unit, so this is about bounding a single task rather than
# about the provider: a sweep that cannot finish in one pass resumes next
# cycle, because the cursor is "messages we have not stored yet".
SYNC_BATCH = int(os.environ.get("JOBTRACKER_MAIL_SYNC_BATCH", "500"))

# Rows read from an archive before flushing. The archive readers stream, so
# this bounds memory rather than the file: 38,685 messages at ~6KB of retained
# body is well over 200MB if accumulated, and these hosts already have
# "cannot allocate memory" as a named transient failure.
IMPORT_FLUSH = int(os.environ.get("JOBTRACKER_MAIL_IMPORT_FLUSH", "500"))

# How far back the sync window reaches before the newest message already
# stored. Not a cursor: mail is delivered late and out of order, and a cursor
# at the newest id would skip those permanently. Two days is generous against
# Gmail's own delivery reordering while still bounding the listing, and
# re-read messages dedupe on write at no cost beyond the fetch.
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
    _set_progress(task_id, 0, len(users), "probing credentials")
    for i, user_id in enumerate(users, 1):
        oauth.get_access_token(user_id)
        _set_progress(task_id, i, len(users), "probing credentials")


async def handle_sync_gmail(task_id: int, payload: dict[str, Any]) -> None:
    """Store messages we do not already have.

    The cursor is the absence of a row rather than a stored historyId or date.
    That is slower per pass and much harder to get wrong: a crashed sweep
    resumes exactly where it stopped, a re-run is a no-op, and nothing can
    skip a message because a cursor advanced past it.

    Run in a thread: every Gmail call and every INSERT below is blocking, and
    this worker's event loop is also driving concurrent AI calls. Holding it
    for a few hundred sequential HTTP round trips would stall those.
    """
    user_ids = [payload["user_id"]] if payload.get("user_id") else connected_user_ids()
    for user_id in user_ids:
        await asyncio.to_thread(_sync_one, task_id, user_id)
    enqueue("classify_mail", {}, dedupe_key=None)


def _sync_one(task_id: int, user_id: int) -> None:
    stored = 0
    pending: list[Any] = []
    seen = _stored_ids(user_id)
    for message_id in gmail.list_message_ids(user_id, after=_since(user_id)):
        if stored >= SYNC_BATCH:
            break
        message = gmail.fetch_message(user_id, message_id)
        # The list gives Gmail's own ids; dedupe is on the RFC Message-ID, so
        # this is the earliest point the two can be compared. Re-fetching a
        # message we already hold costs one quota unit and no write.
        if message.provider_message_id in seen:
            continue
        pending.append(message)
        stored += 1
        if len(pending) >= IMPORT_FLUSH:
            mail_store.store_messages(user_id, pending)
            pending = []
            _set_progress(task_id, stored, SYNC_BATCH, "gmail sync")
    if pending:
        mail_store.store_messages(user_id, pending)
    _set_progress(task_id, stored, SYNC_BATCH, "gmail sync")
    logger.info(f"gmail sync: stored {stored} new message(s) for user {user_id}")


def _since(user_id: int) -> str | None:
    """A Gmail `after:` bound, or None to walk the whole mailbox.

    Without this, every sync lists all 38,685 ids and fetches each one only to
    find it already stored - the mailbox re-read, daily, forever.

    It is a WINDOW, not a cursor. The bound is set back by SYNC_OVERLAP_DAYS
    from the newest message already held, so mail that arrives out of order,
    or is delivered late, still falls inside it. A cursor advanced to the
    newest id would skip those permanently; overlapping re-reads a handful of
    messages that then dedupe on write, which is the cheap direction to be
    wrong in.
    """
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


def _stored_ids(user_id: int) -> set[str]:
    return {
        r["provider_message_id"]
        for r in db.query(
            "SELECT provider_message_id FROM email_messages WHERE user_id = %s", (user_id,)
        )
    }


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
            _set_progress(task_id, stored, 0, f"importing {path.name}")
    if pending:
        stored += mail_store.store_messages(user_id, pending)
    _set_progress(task_id, stored, stored, f"imported {path.name}")
    logger.info(f"archive import: {stored} message(s) from {path.name} for user {user_id}")
