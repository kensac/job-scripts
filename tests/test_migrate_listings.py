"""The listings backfill: inline text and raw records move to the bundles the
writer produces, are verified there, and come back exactly as they were."""

from __future__ import annotations

import dataclasses

import pytest

from api import db
from core import listing_payloads
from core.listing_payloads import COLUMNS, migrate, resolve
from core.payload_objects import PayloadStore
from tests.factories import ObjectClient
from tests.test_listings import LISTED, LONG_RAW, LONG_TEXT, _ingest

TEXTED = [
    dataclasses.replace(LISTED[0], description=LONG_TEXT, raw=LONG_RAW),
    dataclasses.replace(LISTED[1], raw={"id": 2}),
    dataclasses.replace(LISTED[2], description="Short text", raw={"id": 3}),
]


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda *a, **kw: store)
    return store


@pytest.fixture
def inline(monkeypatch, f, objects):
    """Three rows written inline, as a pull during an outage or before the
    reference writer leaves them; every row exactly as stored."""
    f.make_source("acme")
    objects.client.fail_put = True
    _ingest(monkeypatch, f, TEXTED)
    objects.client.fail_put = False
    return _rows()


def _rows() -> list[dict]:
    return db.query("SELECT * FROM listings ORDER BY url")


def _run(mode, **kw):
    return migrate(mode, **{"after": "", "limit": 10, "chunk_size": 2, **kw})


def test_externalize_verify_restore_round_trip(inline, objects):
    assert _run("count")["counts"] == {"inline": 3}

    result = _run("externalize")

    assert result["counts"] == {"externalized": 3}
    assert result["exhausted"] is True and result["failed"] == []
    # One bundle per page of two rows, not one object per value.
    assert len(objects.client.objects) == 2
    rows = _rows()
    assert all(row["raw"] == {} and row["description"] == "" for row in rows)
    resolved = resolve(db.query(f"SELECT url, {COLUMNS} FROM listings ORDER BY url"))
    assert [(r["description"], r["raw"]) for r in resolved] == [
        (row["description"], row["raw"]) for row in inline
    ]
    assert _run("count")["counts"] == {"referenced": 3}
    assert _run("verify")["counts"] == {"verified": 3}
    # Converted rows are skipped when selected again.
    assert _run("externalize")["counts"] == {"referenced": 3}

    assert _run("restore")["counts"] == {"restored": 3}

    assert _rows() == inline


def test_an_untexted_row_moves_only_its_raw_record(inline):
    _run("externalize")
    row = db.query_one(f"SELECT {COLUMNS} FROM listings WHERE url = %s", (LISTED[1].url,))
    assert row is not None
    assert row["description_object"] is None and row["raw_object"] is not None


def test_a_row_rewritten_between_upload_and_lock_is_left_and_reported(inline, objects):
    rewritten = TEXTED[0].url

    def pull_rewrites_it():
        db.execute("UPDATE listings SET raw = '{\"id\": 99}' WHERE url = %s", (rewritten,))

    objects.client.after_put = pull_rewrites_it
    result = _run("externalize")

    assert result["counts"] == {"changed": 1, "externalized": 2}
    assert result["failed"] == [rewritten]
    row = db.query_one(f"SELECT {COLUMNS} FROM listings WHERE url = %s", (rewritten,))
    assert row is not None
    assert row["raw"] == {"id": 99} and row["raw_object"] is None
    assert row["description"] == LONG_TEXT


def test_an_upload_failure_holds_the_cursor_and_writes_nothing(inline, objects):
    objects.client.fail_put = True

    result = _run("externalize")

    assert result["counts"] == {"unavailable": 3}
    assert result["after"] == "" and result["exhausted"] is False
    assert _rows() == inline


def test_verify_finds_a_reference_whose_object_is_gone(inline, objects):
    _run("externalize")
    objects.client.objects.clear()

    result = _run("verify")

    assert result["counts"] == {"unavailable": 3}
    assert sorted(result["failed"]) == sorted(p.url for p in TEXTED)
    # Restore cannot write back what it cannot read, and stops before it.
    assert _run("restore")["counts"] == {"unavailable": 3}


def test_the_cursor_walks_the_table_in_url_order(inline):
    urls = sorted(p.url for p in TEXTED)
    first = _run("externalize", limit=2)
    assert (first["after"], first["exhausted"]) == (urls[1], False)
    second = _run("externalize", after=first["after"], limit=2)
    assert second["counts"] == {"externalized": 1}
    assert (second["after"], second["exhausted"]) == (urls[2], True)


def test_a_row_locked_by_a_pull_is_skipped_rather_than_waited_for(inline):
    """The upsert locks in the order the board listed; waiting on it could
    deadlock, and the pull is about to rewrite the row anyway."""
    import psycopg

    from core.pool import DATABASE_URL

    with psycopg.connect(DATABASE_URL) as holder:
        holder.execute("SELECT 1 FROM listings WHERE url = %s FOR UPDATE", (TEXTED[2].url,))
        result = listing_payloads.migrate("externalize", after="", limit=10, chunk_size=10)
        holder.rollback()

    assert result["counts"] == {"externalized": 2, "changed": 1}
