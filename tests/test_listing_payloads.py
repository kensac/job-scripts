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
from tests.test_listings import LISTED, LONG_RAW, LONG_TEXT, _ingest, _versions

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
    [row] = resolve([_row(url)])
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


def _pull(monkeypatch, f, listed) -> dict:
    """One ingest of `listed`; the counts it left on its task."""
    _ingest(monkeypatch, f, listed)
    task = db.query_one("SELECT progress FROM tasks ORDER BY id DESC LIMIT 1")
    assert task is not None
    return task["progress"]


def _referenced(url: str) -> tuple[bool, bool]:
    row = _row(url)
    return row["description_object"] is not None, row["raw_object"] is not None


def test_a_pull_stores_text_and_raw_as_one_verified_object_and_nothing_inline(
    monkeypatch, f, objects
):
    f.make_source("acme")
    untexted = LISTED[1]
    progress = _pull(monkeypatch, f, [POSTING, untexted])

    # One object for the whole pull, not one per value.
    assert len(objects.client.objects) == 1
    assert progress["listings_inline"] == 0
    row = _row(POSTING.url)
    assert (row["description"], row["raw"]) == ("", {})
    assert _referenced(POSTING.url) == (True, True)
    # No text carried: nothing to reference, the raw record still is.
    assert _referenced(untexted.url) == (False, True)
    resolved = {r["url"]: r for r in resolve(db.query(f"SELECT url, {COLUMNS} FROM listings"))}
    assert (resolved[POSTING.url]["description"], resolved[POSTING.url]["raw"]) == (
        LONG_TEXT,
        LONG_RAW,
    )
    assert (resolved[untexted.url]["description"], resolved[untexted.url]["raw"]) == ("", {})


def test_a_repull_of_identical_content_uploads_and_writes_nothing(monkeypatch, f, objects):
    """Unchanged content is recognised by its digest: no object, no new row
    version, and an empty description still keeps the stored text."""
    f.make_source("acme")
    _pull(monkeypatch, f, [POSTING, LISTED[1]])
    before = _versions()
    stored = dict(objects.client.objects)
    assert len(stored) == 1

    _pull(monkeypatch, f, [POSTING, LISTED[1]])
    _pull(monkeypatch, f, [dataclasses.replace(POSTING, description=""), LISTED[1]])

    assert _versions() == before
    assert objects.client.objects == stored


def test_a_changed_raw_record_uploads_only_itself(monkeypatch, f, objects):
    f.make_source("acme")
    _pull(monkeypatch, f, [POSTING])
    description_object = _row(POSTING.url)["description_object"]

    _pull(monkeypatch, f, [dataclasses.replace(POSTING, raw={"id": 9})])

    assert len(objects.client.objects) == 2
    [new] = [raw for raw in objects.client.objects.values() if LONG_TEXT.encode() not in raw]
    assert b'{"id":9}' in new
    row = _row(POSTING.url)
    assert row["description_object"] == description_object
    [resolved] = resolve([row])
    assert (resolved["description"], resolved["raw"]) == (LONG_TEXT, {"id": 9})


def test_a_storage_outage_keeps_the_pull_inline_and_the_next_pull_moves_it(monkeypatch, f, objects):
    """Listings are an archive nothing downstream reads, so an outage must not
    fail the pull that also feeds the catalog. The values go inline, the
    pull says how many, and nothing is left half moved: the next pull that
    lists the row moves it, and the backfill reaches any it never lists."""
    f.make_source("acme")
    objects.client.fail_put = True

    progress = _pull(monkeypatch, f, [POSTING, LISTED[1]])

    # The text, its raw record, and the untexted posting's raw record.
    assert progress["listings_inline"] == 3
    assert _referenced(POSTING.url) == (False, False)
    row = _row(POSTING.url)
    assert (row["description"], row["raw"]) == (LONG_TEXT, LONG_RAW)
    assert db.query_one("SELECT count(*) AS n FROM jobs WHERE source = 'acme'")["n"] == 2

    objects.client.fail_put = False
    progress = _pull(monkeypatch, f, [POSTING, LISTED[1]])

    assert progress["listings_inline"] == 0
    assert _referenced(POSTING.url) == (True, True)
    [resolved] = resolve([_row(POSTING.url)])
    assert (resolved["description"], resolved["raw"]) == (LONG_TEXT, LONG_RAW)


def test_a_pull_without_text_leaves_an_inline_description_for_the_backfill(monkeypatch, f, objects):
    """Nothing in hand to compare it with, so the stored text stays where it
    is; the raw record beside it moves."""
    f.make_source("acme")
    objects.client.fail_put = True
    _pull(monkeypatch, f, [POSTING])
    objects.client.fail_put = False

    _pull(monkeypatch, f, [dataclasses.replace(POSTING, description="")])

    assert _referenced(POSTING.url) == (False, True)
    [resolved] = resolve([_row(POSTING.url)])
    assert (resolved["description"], resolved["raw"]) == (LONG_TEXT, LONG_RAW)
