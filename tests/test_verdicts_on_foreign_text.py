"""An answer read off a page that is not the posting is no evidence about it
(migration 835b21f59b79), and a fetch that brought back such a page brought
back no posting text."""

from __future__ import annotations

import asyncio

import pytest

from api import db
from api.ai import verdicts
from core import latest_posting_checks, page_fetches, verdict_reads
from core.payloads.objects import PayloadStore
from tasks import page_text_judging
from tests.factories import ObjectClient, make_task

NOTICE = (
    "English\nWorkday is currently unavailable.\nWe are experiencing a service interruption.\n"
    "Your service will be restored as quickly as possible. " * 4
)


@pytest.fixture
def objects(monkeypatch):
    held = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda *a, **kw: held)
    return held


def _notice_for(f, title: str) -> tuple[str, int]:
    url = f"https://jobs.test/{title}"
    f.make_job(url=url, title=title)
    return url, page_fetches.record(url, "passed", "scraped", NOTICE)


def _latest(url: str, check: str, prompt_hash: str | None = None) -> str | None:
    sql = verdict_reads.latest("%s", check, prompt_hash=None if prompt_hash is None else "%s")
    params = (url,) if prompt_hash is None else (url, prompt_hash)
    row = db.query_one(f"SELECT ({sql}) AS status", params)
    return row["status"] if row else None


def test_answers_read_off_a_rejected_page_do_not_count(f, objects):
    _notice_for(f, "Paralegal")
    url, notice = _notice_for(f, "Software Engineer")
    assert notice in page_fetches.rejected([notice])

    f.make_verdict(url, "custom", "rejected", prompt_hash="p", page_fetch_id=notice)
    f.make_verdict(url, "clearance", "passed", page_fetch_id=notice)
    f.make_verdict(url, "closed", "passed", page_fetch_id=notice)
    assert _latest(url, "custom", "p") is None
    assert _latest(url, "clearance") is None
    assert _latest(url, "closed") is None


def test_a_closure_read_off_a_rejected_page_still_counts(f, objects):
    _notice_for(f, "Paralegal")
    url, notice = _notice_for(f, "Software Engineer")
    f.make_verdict(url, "closed", "rejected", page_fetch_id=notice)
    assert _latest(url, "closed") == "rejected"


def test_a_page_rejected_later_takes_its_answers_with_it(f, objects):
    early, first = _notice_for(f, "Paralegal")
    f.make_verdict(early, "clearance", "passed", page_fetch_id=first)
    latest_posting_checks.fold(everything=True)
    assert _latest(early, "clearance") == "passed"

    _notice_for(f, "Software Engineer")
    task_id = make_task(page_text_judging.KIND, status="running")
    asyncio.run(page_text_judging.handle_judge_page_texts(task_id, {}))

    assert _latest(early, "clearance") is None
    # The projection is marked to fold again, so no reader sees the old answer.
    assert (
        db.query_one("SELECT 1 FROM latest_posting_checks_dirty WHERE url = %s", (early,))
        is not None
    )


def test_a_page_that_is_not_the_posting_is_not_its_content(f, objects):
    _notice_for(f, "Paralegal")
    url = "https://jobs.test/Software Engineer"
    f.make_job(url=url, title="Software Engineer")
    notice = page_fetches.record(url, "passed", "scraped", NOTICE)
    page = asyncio.run(verdicts._page(notice, NOTICE))
    assert not page.own
