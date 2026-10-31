"""A page fetch's text held by reference to a verified bundle object, and the
readers that must see it as if it were inline (core.page_fetches.resolve)."""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from api import db
from core import page_fetches, store
from core.payloads.objects import PayloadStore, PayloadUnavailable, encode_payload
from tasks.derivations import compensation
from tests.factories import ObjectClient

TEXT = "Senior engineer. Build the data platform. " * 20
URL = "https://example.test/held"


@pytest.fixture
def objects(monkeypatch):
    held = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda *a, **kw: held)
    return held


def _hold(objects: PayloadStore, fetch_id: int) -> None:
    """Move one fetch's text into a bundle, the shape the mover leaves."""
    row = db.query_one("SELECT content FROM page_fetches WHERE id = %s", (fetch_id,))
    assert row is not None
    text = row["content"]
    member = hashlib.sha256(encode_payload(text)).hexdigest()
    ref = objects.put_bundle({member: text})[member]
    db.execute(
        "UPDATE page_fetches SET content = NULL, content_sha256 = %s, content_object = %s, "
        "content_object_size = %s, content_chars = %s WHERE id = %s",
        (bytes.fromhex(member), bytes.fromhex(ref.sha256), ref.size, len(text), fetch_id),
    )


def test_a_held_fetch_reads_as_its_text_through_every_helper(f, objects):
    fetch_id = f.make_fetch(URL, content=TEXT)
    _hold(objects, fetch_id)

    assert db.query_one("SELECT input_content FROM page_texts WHERE id = %s", (fetch_id,)) == {
        "input_content": None
    }
    assert store.get_content(URL) == TEXT
    assert store.get_contents([URL])[URL] == store.Page(fetch_id, TEXT)
    assert page_fetches.text_of(fetch_id) == TEXT
    # The shared lookup still picks it: its length is stored beside the link.
    [row] = page_fetches.resolve(
        db.query(
            "SELECT q.id, q.input_content, q.content_ref FROM (SELECT %s::text AS url) j "
            + store.CONTENT_LATERAL.format(url="j.url", columns="id, input_content, content_ref"),
            (URL,),
        )
    )
    assert (row["id"], row["input_content"]) == (fetch_id, TEXT)


def test_a_link_that_cannot_be_read_fails_instead_of_reading_empty(f, objects):
    fetch_id = f.make_fetch(URL, content=TEXT)
    _hold(objects, fetch_id)
    objects.client.objects.clear()
    with pytest.raises(PayloadUnavailable):
        store.get_content(URL)


def test_a_half_moved_row_is_refused(f):
    fetch_id = f.make_fetch(URL, content=TEXT)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE page_fetches SET content_chars = 5 WHERE id = %s", (fetch_id,))


def test_the_admin_detail_rebuilds_an_answer_from_held_text(client, admin_headers, f, objects):
    fetch_id = f.make_fetch(URL, content=TEXT)
    answer_id = store.add_ai_result(
        URL,
        "passed",
        check_type="custom",
        prompt_hash="filter-key",
        company="Acme",
        job_title="Engineer",
        page_fetch_id=fetch_id,
    )
    before = client.get(f"/v1/admin/queries/{answer_id}", headers=admin_headers).json()
    _hold(objects, fetch_id)

    after = client.get(f"/v1/admin/queries/{answer_id}", headers=admin_headers).json()
    assert after["input_content"] == before["input_content"]
    assert TEXT in after["input_content"]
    fetch = client.get(f"/v1/admin/queries/{fetch_id}", headers=admin_headers).json()
    assert fetch["input_content"] == TEXT


def test_the_comp_hash_is_the_one_sql_computed():
    text = "Pay: $150,000 to $180,000 a year. Ünïcode survives. " * 400
    row = db.query_one(
        "SELECT encode(sha256(convert_to(left(%s, %s), 'UTF8')), 'hex') AS h",
        (text, compensation.COMP_INPUT_CHARS),
    )
    assert row is not None
    assert compensation._hash(text) == row["h"]
