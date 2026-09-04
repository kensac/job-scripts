"""handle_ingest_source: what reaches the catalog from a source row."""

from __future__ import annotations

import asyncio

from api import db, verdicts
from api.tasks import ingest
from core import boards
from core.pittcsc_simplify import JobPosting


def _posting(title: str) -> JobPosting:
    return JobPosting(
        company="",
        locations=["Long Beach, CA"],
        title=title,
        url=f"https://job-boards.greenhouse.io/rocketlab/jobs/{abs(hash(title)) % 10**8}",
        terms=[],
        active=True,
        date_posted=0,
        raw_url="",
    )


def test_title_pattern_keeps_the_rest_of_the_board_out_of_the_catalog(monkeypatch, f):
    """A company board lists every opening. The source's pattern is applied
    before the upsert, so the senior roles never become rows, never get their
    pages cached, and never reach verify_new."""
    f.make_source("rocketlab")
    db.execute(
        "UPDATE sources SET company = 'Rocket Lab', title_pattern = %s WHERE name = 'rocketlab'",
        (r"engineer i\b|intern|new grad",),
    )
    calls = []

    def fetch_listings(url, company=None):
        calls.append((url, company))
        return [
            _posting("Avionics Design Engineer I"),
            _posting("Senior Avionics Design Engineer"),
            _posting("Avionics Development Intern - Electron"),
        ]

    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(boards, "fetch_listings", fetch_listings)
    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "rocketlab"})

    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "rocketlab"}))

    titles = {r["title"] for r in db.query("SELECT title FROM jobs WHERE source = 'rocketlab'")}
    assert titles == {"Avionics Design Engineer I", "Avionics Development Intern - Electron"}
    # The company on the source row is what the fetcher was handed.
    assert calls == [("https://rocketlab.test/jobs.json", "Rocket Lab")]
