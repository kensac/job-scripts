"""A redirector's link is resolved to the posting it points to, once
(core.fetching.redirects), and ingest stores the posting under its own URL
(tasks.ingest)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from api import db
from api.ai import verdicts
from core.fetching import boards, client, redirects
from core.fetching.posting import JobPosting
from tasks import ingest

REAL = "https://job-boards.greenhouse.io/acme/jobs/123"


class _Reply:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status_code = status
        self.headers = {"location": location} if location else {}


@pytest.fixture
def links(monkeypatch):
    """go.vansh.nyc/a redirects to REAL through a second short link; /dead
    answers 404. Every request is counted."""
    asked: list[str] = []
    answers = {
        "https://go.vansh.nyc/a": _Reply(302, "/b"),
        "https://go.vansh.nyc/b": _Reply(302, REAL),
        "https://go.vansh.nyc/dead": _Reply(404),
    }

    def get(url, allow_redirects=True, **kw):
        assert allow_redirects is False
        asked.append(url)
        return answers[url]

    monkeypatch.setattr(client.session, "get", get)
    return asked


def test_a_link_is_followed_to_the_posting_and_asked_once(links):
    assert redirects.resolve(["https://go.vansh.nyc/a", REAL]) == {"https://go.vansh.nyc/a": REAL}
    assert links == ["https://go.vansh.nyc/a", "https://go.vansh.nyc/b"]
    assert redirects.resolve(["https://go.vansh.nyc/a"]) == {"https://go.vansh.nyc/a": REAL}
    assert len(links) == 2


def test_a_link_that_does_not_redirect_is_asked_again_later(links):
    assert redirects.resolve(["https://go.vansh.nyc/dead"]) == {}
    assert redirects.resolve(["https://go.vansh.nyc/dead"]) == {}
    assert links == ["https://go.vansh.nyc/dead", "https://go.vansh.nyc/dead"]


def _posting(url: str) -> JobPosting:
    return JobPosting(
        company="Acme",
        locations=["Remote"],
        title="Software Engineer",
        url=url,
        terms=[],
        active=True,
        date_posted=0,
        raw_url=url,
    )


def _ingest(f, monkeypatch, postings: list[JobPosting]) -> None:
    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: postings)
    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "vansh"})
    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "vansh"}))


def test_ingest_stores_the_posting_under_its_own_url_and_retires_the_link(f, monkeypatch, links):
    f.make_source("vansh")
    short = "https://go.vansh.nyc/a"
    # Stored under the link before links were resolved.
    monkeypatch.setattr(redirects, "REDIRECTORS", frozenset())
    _ingest(f, monkeypatch, [_posting(short)])
    assert db.query_one("SELECT available FROM jobs WHERE url = %s", (short,))["available"]

    monkeypatch.setattr(redirects, "REDIRECTORS", frozenset({"go.vansh.nyc"}))
    _ingest(f, monkeypatch, [_posting(short), dataclasses.replace(_posting(REAL), raw_url=REAL)])

    real = db.query_one("SELECT raw_url, available FROM jobs WHERE url = %s", (REAL,))
    assert real is not None and real["available"]
    # The link's row is unlisted by the source that now lists the posting.
    assert not db.query_one("SELECT available FROM jobs WHERE url = %s", (short,))["available"]
