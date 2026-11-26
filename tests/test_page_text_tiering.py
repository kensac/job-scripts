"""A page's text lives in object storage: uploaded before its fetch row is
written, and moved there by tasks.page_text_tiering when an outage or an
older release left it inline."""

from __future__ import annotations

import asyncio

import pytest

from api import db
from core import page_fetches, store
from core.payloads.objects import PayloadStore, PayloadUnavailable
from tasks import page_text_tiering
from tests.factories import ObjectClient, make_task

PAGE = "Build the platform. " * 30


@pytest.fixture
def objects(monkeypatch):
    held = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda *a, **kw: held)
    return held


def _stored(fetch_id: int) -> dict:
    row = db.query_one(
        "SELECT content, content_object, content_chars FROM page_fetches WHERE id = %s",
        (fetch_id,),
    )
    assert row is not None
    return row


def _inline(url: str, content: str) -> int:
    """A fetch written inline, as an outage or an older release leaves it."""
    row = db.query_one(
        "INSERT INTO page_fetches (url, status, method, content) "
        "VALUES (%s, 'passed', 'scraped', %s) RETURNING id",
        (url, content),
    )
    assert row is not None
    return row["id"]


def _run() -> None:
    task_id = make_task("tier_page_text", status="running")
    asyncio.run(page_text_tiering.handle_tier_page_text(task_id, {}))


def test_a_fetch_writes_its_text_by_reference(f, objects):
    url = "https://jobs.test/written"
    f.make_job(url=url)
    fetch = page_fetches.record(url, "passed", "scraped", PAGE)

    row = _stored(fetch)
    assert row["content"] is None and row["content_object"] is not None
    assert row["content_chars"] == len(PAGE)
    assert store.get_content(url) == PAGE
    assert page_fetches.text_of(fetch) == PAGE


def test_many_fetches_keep_their_order_and_share_bundles(f, objects):
    fetches = [
        page_fetches.Fetch(f"https://jobs.test/many/{i}", "passed", "listing text", f"{PAGE}{i}")
        for i in range(5)
    ] + [page_fetches.Fetch("https://jobs.test/many/failed", "failed", "fetch returned nothing")]
    ids = page_fetches.record_many(fetches)

    assert ids == sorted(ids) and len(ids) == len(fetches)
    for fetch, fetch_id in zip(fetches[:5], ids[:5], strict=True):
        assert page_fetches.text_of(fetch_id) == fetch.content
    assert len({_stored(i)["content_object"] for i in ids[:5]}) == 1
    failed = _stored(ids[5])
    assert failed["content"] is None and failed["content_object"] is None


def test_an_outage_stores_the_text_inline_and_the_move_catches_it_up(f, objects):
    url = "https://jobs.test/outage"
    f.make_job(url=url)
    objects.client.fail_put = True
    fetch = page_fetches.record(url, "passed", "scraped", PAGE)
    assert _stored(fetch)["content"] == PAGE
    assert store.get_content(url) == PAGE

    objects.client.fail_put = False
    _run()
    row = _stored(fetch)
    assert row["content"] is None and row["content_chars"] == len(PAGE)
    assert store.get_content(url) == PAGE


def test_the_move_takes_every_inline_row_and_leaves_failed_fetches(f, objects):
    urls = [f"https://jobs.test/old/{i}" for i in range(3)]
    old = [_inline(url, f"{PAGE}{url}") for url in urls]
    failed = page_fetches.record("https://jobs.test/old/failed", "failed", "fetch returned nothing")

    _run()
    assert all(_stored(i)["content"] is None for i in old)
    assert [page_fetches.text_of(i) for i in old] == [f"{PAGE}{url}" for url in urls]
    assert _stored(failed) == {"content": None, "content_object": None, "content_chars": None}
    assert page_text_tiering.inline_count() == 0


def test_a_move_that_cannot_upload_clears_nothing(f, objects):
    fetch = _inline("https://jobs.test/unmoved", PAGE)
    objects.client.fail_put = True
    with pytest.raises(PayloadUnavailable):
        _run()
    assert _stored(fetch)["content"] == PAGE


def test_resolve_reads_each_bundle_once(f, objects, monkeypatch):
    ids = page_fetches.record_many(
        [
            page_fetches.Fetch(f"https://jobs.test/once/{i}", "passed", "scraped", PAGE)
            for i in range(4)
        ]
    )
    reads = []
    original = objects.client.get_object

    def counted(**kwargs):
        reads.append(kwargs["Key"])
        return original(**kwargs)

    monkeypatch.setattr(objects.client, "get_object", counted)
    rows = page_fetches.resolve(
        db.query("SELECT id, input_content, content_ref FROM page_texts WHERE id = ANY(%s)", (ids,))
    )
    assert [r["input_content"] for r in rows] == [PAGE] * 4
    assert len(reads) == 1


def test_a_board_run_reads_text_only_for_jobs_still_needing_a_verdict(f, objects, monkeypatch):
    from tasks import managed_boards

    decided_fetch = page_fetches.record("https://jobs.test/board/decided", "passed", "scraped", "A")
    open_fetch = page_fetches.record("https://jobs.test/board/open", "passed", "scraped", PAGE)
    read = []
    original = page_fetches.resolve

    def counted(rows, *args, **kwargs):
        rows = list(rows)
        read.extend(row["id"] for row in rows)
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(page_fetches, "resolve", counted)
    jobs = managed_boards._with_pages(
        [
            {"url": "https://jobs.test/board/decided", "content_query_id": decided_fetch},
            {"url": "https://jobs.test/board/open", "content_query_id": open_fetch},
            {"url": "https://jobs.test/board/none", "content_query_id": None},
        ],
        {"https://jobs.test/board/decided"},
    )
    assert read == [open_fetch]
    assert [job["content"] for job in jobs] == ["", PAGE, ""]


def test_the_move_waits_while_an_older_worker_runs(f, objects):
    from api import data_level

    fetch = _inline("https://jobs.test/waiting", PAGE)
    db.execute(
        "INSERT INTO worker_status (name, release, data_level, data_level_release, last_seen) "
        "VALUES ('old', 'r1', %s, 'r1', now())",
        (data_level.LEVEL - 1,),
    )
    _run()
    assert _stored(fetch)["content"] == PAGE

    db.execute("UPDATE worker_status SET data_level = %s WHERE name = 'old'", (data_level.LEVEL,))
    _run()
    assert _stored(fetch)["content"] is None


def test_switching_the_move_off_leaves_inline_text_where_it_is(f, objects, set_config):
    fetch = _inline("https://jobs.test/paused", PAGE)
    set_config("page_text_move_enabled", False)
    _run()
    assert _stored(fetch)["content"] == PAGE
    set_config("page_text_move_enabled", True)
    _run()
    assert _stored(fetch)["content"] is None
