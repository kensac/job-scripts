"""Keeping the markup, and the ordering that makes it safe."""

from __future__ import annotations

import pytest

from api import db
from api.tasks import message_html


def _msg(uid: int, mid: str, body: str, html: str | None = None) -> int:
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at, body_text, body_html) "
        "VALUES (%s, %s, 'olm', 'a@b.com', 's', now(), %s, %s) RETURNING id",
        (uid, mid, body, html),
    )
    assert row is not None
    return row["id"]


HTML = "<html><body><p>Thanks for applying.</p></body></html>"


@pytest.mark.asyncio
async def test_the_markup_is_kept_before_the_text_that_held_it_is_replaced(f):
    """The ordering this whole module exists for.

    body_text was the ONLY copy of the markup - the import streams the archive
    and keeps nothing, and no other column holds it. Deriving the text first
    would have destroyed it permanently, including for the messages the reader
    exists to render.
    """
    uid = f.make_user()
    mid = _msg(uid, "m1", HTML)
    task_id = f.make_task("backfill_message_html", {}, status="running")

    await message_html.handle_backfill_message_html(task_id, {})

    row = db.query_one("SELECT body_text, body_html FROM email_messages WHERE id = %s", (mid,))
    assert row is not None
    assert row["body_html"] == HTML, "the markup must survive"
    assert "Thanks for applying." in row["body_text"]
    assert "<" not in row["body_text"]


@pytest.mark.asyncio
async def test_a_plain_body_is_left_entirely_alone(f):
    uid = f.make_user()
    mid = _msg(uid, "m2", "Thanks for applying. We will be in touch.")
    await message_html.handle_backfill_message_html(
        f.make_task("backfill_message_html", {}, status="running"), {}
    )

    row = db.query_one("SELECT body_text, body_html FROM email_messages WHERE id = %s", (mid,))
    assert row is not None
    assert row["body_text"] == "Thanks for applying. We will be in touch."
    assert row["body_html"] is None, "no markup existed, so none is invented"


@pytest.mark.asyncio
async def test_running_it_twice_changes_nothing(f):
    """Idempotent by predicate, not bookkeeping: a row qualifies only while its
    body still holds markup and its html is still empty."""
    uid = f.make_user()
    mid = _msg(uid, "m3", HTML)
    task = f.make_task("backfill_message_html", {}, status="running")
    await message_html.handle_backfill_message_html(task, {})
    first = db.query_one("SELECT body_text, body_html FROM email_messages WHERE id = %s", (mid,))

    assert message_html.pending_count() == 0
    await message_html.handle_backfill_message_html(task, {})
    again = db.query_one("SELECT body_text, body_html FROM email_messages WHERE id = %s", (mid,))
    assert again == first


@pytest.mark.asyncio
async def test_a_message_that_already_has_markup_kept_is_not_touched(f):
    """A row imported after the column existed already holds both fields. It
    must not be re-derived, or its text would be converted twice."""
    uid = f.make_user()
    mid = _msg(uid, "m4", "Thanks for applying.", HTML)
    await message_html.handle_backfill_message_html(
        f.make_task("backfill_message_html", {}, status="running"), {}
    )

    row = db.query_one("SELECT body_text, body_html FROM email_messages WHERE id = %s", (mid,))
    assert row is not None
    assert row["body_text"] == "Thanks for applying."
    assert row["body_html"] == HTML


@pytest.mark.asyncio
async def test_a_document_with_no_readable_text_keeps_its_markup(f):
    """Five of 400 sampled real messages convert to nothing - calendar invites
    whose whole body is `<html><head><meta></head></html>`. The text going
    empty is honest; losing the markup as well would not be."""
    uid = f.make_user()
    empty_doc = '<html><head><meta charset="utf-8"></head></html>'
    mid = _msg(uid, "m5", empty_doc)
    await message_html.handle_backfill_message_html(
        f.make_task("backfill_message_html", {}, status="running"), {}
    )

    row = db.query_one("SELECT body_text, body_html FROM email_messages WHERE id = %s", (mid,))
    assert row is not None
    assert not (row["body_text"] or "").strip()
    assert row["body_html"] == empty_doc


@pytest.mark.asyncio
async def test_it_resumes_rather_than_restarting(f):
    """A partial run leaves the rest still qualifying, so a resumed run picks
    up exactly what is left."""
    uid = f.make_user()
    for i in range(5):
        _msg(uid, f"m6-{i}", HTML)
    assert message_html.pending_count() == 5

    assert message_html._convert_chunk(2) == 2
    assert message_html.pending_count() == 3
    await message_html.handle_backfill_message_html(
        f.make_task("backfill_message_html", {}, status="running"), {}
    )
    assert message_html.pending_count() == 0
