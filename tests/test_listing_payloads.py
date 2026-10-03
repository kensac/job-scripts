"""A listing's description and raw record held inline or by reference to a
verified bundle object, and what a pull does to each shape."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from api import db
from core.listing_payloads import COLUMNS, resolve
from core.payload_objects import PayloadStore, PayloadUnavailable, encode_payload
from tests.factories import ObjectClient
from tests.test_listings import LISTED, LONG_RAW, LONG_TEXT, _ingest

POSTING = dataclasses.replace(LISTED[0], description=LONG_TEXT, raw=LONG_RAW)


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda *a, **kw: store)
    return store


def _row(url: str) -> dict:
    row = db.query_one(f"SELECT {COLUMNS} FROM listings WHERE url = %s", (url,))
    assert row is not None
    return row


def _hold_by_reference(store: PayloadStore, url: str) -> None:
    """Move a row's inline values into one bundle, the shape the reference
    writer and the backfill leave behind."""
    row = _row(url)
    digests = {
        field: hashlib.sha256(encode_payload(row[field])).hexdigest()
        for field in ("description", "raw")
    }
    refs = store.put_bundle({digests[field]: row[field] for field in digests})
    ref = next(iter(refs.values()))
    db.execute(
        "UPDATE listings SET description = '', raw = '{}', "
        "description_sha256 = %(d)s, raw_sha256 = %(r)s, "
        "description_object = %(o)s, raw_object = %(o)s, "
        "description_object_size = %(n)s, raw_object_size = %(n)s WHERE url = %(url)s",
        {
            "d": bytes.fromhex(digests["description"]),
            "r": bytes.fromhex(digests["raw"]),
            "o": bytes.fromhex(ref.sha256),
            "n": ref.size,
            "url": url,
        },
    )


def test_a_referenced_row_resolves_to_what_was_written(monkeypatch, f, objects):
    f.make_source("acme")
    _ingest(monkeypatch, f, [POSTING])
    _hold_by_reference(objects, POSTING.url)

    row = _row(POSTING.url)
    assert (row["description"], row["raw"]) == ("", {})
    [resolved] = resolve([row])
    assert (resolved["description"], resolved["raw"]) == (LONG_TEXT, LONG_RAW)


def test_a_reference_that_reads_something_else_fails_closed(monkeypatch, f, objects):
    f.make_source("acme")
    _ingest(monkeypatch, f, [POSTING])
    _hold_by_reference(objects, POSTING.url)
    # The row names a member of the bundle that is not its own value.
    db.execute("UPDATE listings SET description_sha256 = raw_sha256 WHERE url = %s", (POSTING.url,))
    with pytest.raises(PayloadUnavailable):
        resolve([_row(POSTING.url)])

    objects.client.objects.clear()
    with pytest.raises(PayloadUnavailable):
        resolve([_row(POSTING.url)])


def test_a_pull_without_text_keeps_a_referenced_description(monkeypatch, f, objects):
    """An empty description is the listing not carrying the text, whichever
    shape holds the stored one."""
    f.make_source("acme")
    _ingest(monkeypatch, f, [POSTING])
    _hold_by_reference(objects, POSTING.url)

    _ingest(monkeypatch, f, [dataclasses.replace(POSTING, description="", title="Renamed")])

    [resolved] = resolve([_row(POSTING.url)])
    assert resolved["description"] == LONG_TEXT


def test_a_changed_value_replaces_a_referenced_one_and_never_leaves_a_stale_reference(
    monkeypatch, f, objects
):
    """A writer that stores the new value inline must drop the reference with
    it, or every reader keeps resolving the old value."""
    f.make_source("acme")
    _ingest(monkeypatch, f, [POSTING])
    _hold_by_reference(objects, POSTING.url)

    changed = dataclasses.replace(POSTING, description="New text", raw={"id": 8})
    _ingest(monkeypatch, f, [changed])

    [resolved] = resolve([_row(POSTING.url)])
    assert (resolved["description"], resolved["raw"]) == ("New text", {"id": 8})
