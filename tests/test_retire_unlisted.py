"""A board that stops listing a posting, or a pattern that stops admitting it,
retires the catalog row.

Nothing did this before: a company board's rows stayed active until a
reverify sweep found the page gone, and a row the pattern no longer admitted
stayed active forever. On 2026-09-04, 4,554 of 6,306 active company-board
rows were titles the current pattern would not admit (CVS "Store Associate"
and its kin), each still eligible for every AI sweep.
"""

from __future__ import annotations

import asyncio

from api import db, verdicts
from api.tasks import ingest
from core import boards
from core.pittcsc_simplify import JobPosting

TODAY = 2_000_000_000


def _posting(title: str, url: str) -> JobPosting:
    return JobPosting(
        company="Acme",
        locations=[],
        title=title,
        url=url,
        terms=[],
        active=True,
        date_posted=TODAY,
        raw_url="",
    )


def _ingest(monkeypatch, f, name, listed):
    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: listed)
    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": name}, status="running")
    asyncio.run(ingest.handle_ingest_source(task_id, {"source": name}))


def _active(name: str) -> dict[str, bool]:
    return {
        r["url"]: r["active"]
        for r in db.query("SELECT url, active FROM jobs WHERE source = %s", (name,))
    }


KEPT = _posting("Software Engineer", "https://boards.greenhouse.io/acme/jobs/1")
DROPPED = _posting("Store Associate", "https://boards.greenhouse.io/acme/jobs/2")
GONE = _posting("Data Engineer", "https://boards.greenhouse.io/acme/jobs/3")


def test_a_board_retires_what_it_stops_listing_and_what_the_pattern_stops_admitting(monkeypatch, f):
    f.make_source("acme")
    db.execute(
        "UPDATE sources SET listings_url = 'https://boards-api.greenhouse.io/v1/boards/acme/jobs', "
        "title_pattern = 'engineer' WHERE name = 'acme'"
    )
    _ingest(monkeypatch, f, "acme", [KEPT, DROPPED, GONE])
    # The pattern admitted two; the third never entered the catalog.
    assert _active("acme") == {KEPT.url: True, GONE.url: True}

    # Next pull: the pattern was widened to admit DROPPED, and the board no
    # longer lists GONE.
    db.execute("UPDATE sources SET title_pattern = '' WHERE name = 'acme'")
    _ingest(monkeypatch, f, "acme", [KEPT, DROPPED])
    assert _active("acme") == {KEPT.url: True, DROPPED.url: True, GONE.url: False}

    # Narrowed again: a row the pattern stops admitting retires too, and a
    # posting listed again comes back.
    db.execute("UPDATE sources SET title_pattern = 'engineer' WHERE name = 'acme'")
    _ingest(monkeypatch, f, "acme", [KEPT, DROPPED, GONE])
    assert _active("acme") == {KEPT.url: True, DROPPED.url: False, GONE.url: True}


def test_an_empty_pull_retires_nothing(monkeypatch, f):
    f.make_source("acme")
    db.execute(
        "UPDATE sources SET listings_url = 'https://boards-api.greenhouse.io/v1/boards/acme/jobs' "
        "WHERE name = 'acme'"
    )
    _ingest(monkeypatch, f, "acme", [KEPT])
    _ingest(monkeypatch, f, "acme", [])
    assert _active("acme") == {KEPT.url: True}


def test_an_aggregator_list_is_not_a_closure_signal(monkeypatch, f):
    """A markdown or sheet list trims old rows; absence there says nothing
    about the posting, so only the ATS boards retire on absence."""
    f.make_source("agg")
    _ingest(monkeypatch, f, "agg", [KEPT, GONE])
    _ingest(monkeypatch, f, "agg", [KEPT])
    assert _active("agg") == {KEPT.url: True, GONE.url: True}
