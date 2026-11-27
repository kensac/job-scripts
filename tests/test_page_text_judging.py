"""A page that is not its posting is never the posting's text
(docs/agents/sources-and-boards.md): page_text_rejections names it, page_texts
leaves it out, and readers fall back to the posting's own text."""

from __future__ import annotations

import asyncio

import pytest

from api import db
from core import page_fetches, store
from core.payloads.objects import PayloadStore
from tasks import page_text_judging
from tests.factories import ObjectClient, make_task

NOTICE = (
    "English\nWorkday is currently unavailable.\nWe are experiencing a service interruption.\n"
    "Your service will be restored as quickly as possible. " * 4
)


def _posting(title: str) -> str:
    return f"{title}\nAbout the role\nYou will join the team as our {title} and build things. " * 6


@pytest.fixture
def objects(monkeypatch):
    held = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda *a, **kw: held)
    return held


def _rejected() -> set[int]:
    return {r["fetch_id"] for r in db.query("SELECT fetch_id FROM page_text_rejections")}


def _run() -> int:
    """Run the sweep; where the walk now resumes."""
    task_id = make_task(page_text_judging.KIND, status="running")
    asyncio.run(page_text_judging.handle_judge_page_texts(task_id, {}))
    return page_text_judging._start()


def test_a_role_is_its_words_without_level_numerals():
    assert page_fetches.role("Software Engineer II") == page_fetches.role("software engineer 3")
    assert page_fetches.role("Data Analyst") != page_fetches.role("Software Engineer")


def test_a_page_names_a_role_by_the_start_of_its_words():
    page = "Join our engineering team as a new graduate in artificial intelligence."
    assert page_fetches.names_role("New Grad Engineer", page)
    # Two-letter words are abbreviations a page spells out.
    assert page_fetches.names_role("AI Engineer", page)
    assert not page_fetches.names_role("Data Scientist", page)


def test_a_page_shared_across_roles_that_names_none_is_rejected_when_written(f, objects):
    for title in ("Data Analyst", "Software Engineer"):
        f.make_job(url=f"https://jobs.test/{title}", title=title)
    first = page_fetches.record("https://jobs.test/Data Analyst", "passed", "scraped", NOTICE)
    second = page_fetches.record("https://jobs.test/Software Engineer", "passed", "scraped", NOTICE)
    # The first fetch had no other fetch of its text to be judged against;
    # the second did.
    assert _rejected() == {second}
    assert first not in _rejected()


def test_a_template_that_names_each_role_is_each_postings_text(f, objects):
    template = "We are hiring a Customer Service Associate to help our store. " * 6
    for title in ("Customer Service Associate", "Customer Service Associate II"):
        f.make_job(url=f"https://jobs.test/{title}", title=title)
        page_fetches.record(f"https://jobs.test/{title}", "passed", "scraped", template)
    assert _rejected() == set()


def test_a_boards_own_record_is_never_rejected(f, objects):
    for title in ("Cashier", "Customer Service Associate"):
        f.make_job(url=f"https://jobs.test/{title}", title=title)
        # A renamed feed: the board's record reads the other title.
        page_fetches.record(f"https://jobs.test/{title}", "passed", "ats text", NOTICE)
    assert _rejected() == set()


def test_readers_fall_back_to_the_postings_own_text(f, objects):
    url = "https://jobs.test/own"
    f.make_job(url=url, title="Backend Engineer")
    f.make_job(url="https://jobs.test/other", title="Paralegal")
    own = page_fetches.record(url, "passed", "scraped", _posting("Backend Engineer"))
    page_fetches.record("https://jobs.test/other", "passed", "scraped", NOTICE)
    notice = page_fetches.record(url, "passed", "scraped", NOTICE)

    assert notice in _rejected()
    assert store.get_content(url) == _posting("Backend Engineer")
    assert store.get_contents([url])[url].fetch_id == own
    # What the rejected fetch held is still readable by its id.
    assert page_fetches.text_of(notice) == NOTICE


def test_the_sweep_judges_older_fetches_once_their_text_is_shared(f, objects):
    early = "https://jobs.test/early"
    f.make_job(url=early, title="Paralegal")
    f.make_job(url="https://jobs.test/late", title="Machine Learning Engineer")
    first = page_fetches.record(early, "passed", "scraped", NOTICE)
    db.execute(
        "INSERT INTO posting_embeddings (url, embedding, model, content_row_id) "
        "VALUES (%s, ('[' || array_to_string(array_fill(0.1::real, ARRAY[768]), ',') || ']')"
        "::halfvec, 'm', %s)",
        (early, first),
    )
    second = page_fetches.record("https://jobs.test/late", "passed", "scraped", NOTICE)
    assert _rejected() == {second}

    through = _run()
    assert _rejected() == {first, second}
    # The embedding made from the notice goes with it, so the posting is
    # embedded again from its own text.
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (early,)) is None
    assert through == second


def test_the_sweep_resumes_and_stops_below_inline_text(f, objects):
    f.make_job(url="https://jobs.test/a", title="Paralegal")
    done = page_fetches.record("https://jobs.test/a", "passed", "scraped", _posting("Paralegal"))
    assert _run() == done
    inline = db.query_one(
        "INSERT INTO page_fetches (url, status, method, content) "
        "VALUES ('https://jobs.test/a', 'passed', 'scraped', %s) RETURNING id",
        (_posting("Paralegal"),),
    )
    assert inline is not None
    after = page_fetches.record("https://jobs.test/a", "passed", "scraped", _posting("Paralegal"))
    # The inline row has no digest yet, so the walk waits below it.
    assert _run() == done
    assert after > inline["id"]
