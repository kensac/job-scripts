"""Where a page's text lives follows one rule (tasks.page_text_tiering.COLD):
held by reference once nothing reads it, inline again the moment something
might."""

from __future__ import annotations

import asyncio

import pytest

from api import db
from core import store
from core.payloads.objects import PayloadStore
from tasks import page_text_tiering
from tests.factories import ObjectClient, make_task

PAGE = "Build the platform. " * 30


@pytest.fixture
def objects(monkeypatch):
    held = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda *a, **kw: held)
    return held


@pytest.fixture
def on(set_config):
    set_config("page_text_tiering_enabled", True)
    set_config("page_text_cold_after_days", 14)
    return set_config


def _age(fetch_id: int, days: int) -> None:
    db.execute(
        "UPDATE page_fetches SET created_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, fetch_id),
    )


def _held(fetch_id: int) -> bool:
    row = db.query_one("SELECT content_object FROM page_fetches WHERE id = %s", (fetch_id,))
    assert row is not None
    return row["content_object"] is not None


def _run() -> None:
    task_id = make_task("tier_page_text", status="running")
    asyncio.run(page_text_tiering.handle_tier_page_text(task_id, {}))


def _closed_job(f, url: str) -> None:
    job_id = f.make_job(url=url)
    db.execute("UPDATE jobs SET available = false WHERE id = %s", (job_id,))


def test_an_older_copy_moves_and_the_page_readers_see_does_not(f, objects, on):
    url = "https://jobs.test/refetched"
    f.make_job(url=url)
    old = f.make_fetch(url, content="Old text. " * 40)
    new = f.make_fetch(url, content=PAGE)
    _age(old, 20)
    _age(new, 20)

    _run()
    assert _held(old) and not _held(new)
    assert store.get_content(url) == PAGE


def test_a_closed_posting_moves_and_comes_back_when_it_reopens(f, objects, on):
    url = "https://jobs.test/closed"
    _closed_job(f, url)
    fetch = f.make_fetch(url, content=PAGE)
    _age(fetch, 20)

    _run()
    assert _held(fetch)
    assert store.get_content(url) == PAGE

    db.execute("UPDATE jobs SET available = true WHERE url = %s", (url,))
    _run()
    assert not _held(fetch)
    row = db.query_one("SELECT content FROM page_fetches WHERE id = %s", (fetch,))
    assert row == {"content": PAGE}


def test_a_recent_or_open_page_stays(f, objects, on):
    recent = "https://jobs.test/recent-close"
    _closed_job(f, recent)
    young = f.make_fetch(recent, content=PAGE)
    _age(young, 5)
    open_url = "https://jobs.test/open"
    f.make_job(url=open_url)
    old_open = f.make_fetch(open_url, content=PAGE)
    _age(old_open, 60)

    _run()
    assert not _held(young) and not _held(old_open)


def test_a_longer_window_or_switching_off_brings_pages_back(f, objects, on):
    url = "https://jobs.test/window"
    _closed_job(f, url)
    fetch = f.make_fetch(url, content=PAGE)
    _age(fetch, 20)
    _run()
    assert _held(fetch)

    on("page_text_cold_after_days", 30)
    _run()
    assert not _held(fetch)

    on("page_text_cold_after_days", 14)
    _run()
    assert _held(fetch)
    on("page_text_tiering_enabled", False)
    _run()
    assert not _held(fetch)


def test_nothing_moves_while_switched_off(f, objects):
    url = "https://jobs.test/off"
    _closed_job(f, url)
    fetch = f.make_fetch(url, content=PAGE)
    _age(fetch, 60)
    _run()
    assert not _held(fetch)
