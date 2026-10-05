"""A posting is never active only because of a source nobody pulls."""

from __future__ import annotations

import asyncio

from api import db, worker
from core import catalog
from tasks import ingest


def _listed(url: str, source: str, *, kept: bool) -> None:
    db.execute(
        "INSERT INTO listings (url, source, pattern, kept) VALUES (%s, %s, '', %s)",
        (url, source, kept),
    )


def _active(job_id: int) -> bool:
    return db.query_one("SELECT active FROM jobs WHERE id = %s", (job_id,))["active"]


def _events(job_id: int) -> list[bool]:
    return [
        r["listed"]
        for r in db.query(
            "SELECT listed FROM job_listing_events WHERE job_id = %s ORDER BY id", (job_id,)
        )
    ]


def test_switched_off_source_postings_are_retired_unless_an_active_source_admits_them(f):
    f.make_source("off", active=False)
    f.make_source("on", active=True)
    alone = f.make_job(source="off", url="https://jobs.test/alone")
    shared = f.make_job(source="off", url="https://jobs.test/shared")
    screened = f.make_job(source="off", url="https://jobs.test/screened")
    by_off = f.make_job(source="off", url="https://jobs.test/by-off")
    on_job = f.make_job(source="on", url="https://jobs.test/on")
    # Another switched-on source lists and admits it: its next pull would
    # put it straight back, so it stays.
    _listed("https://jobs.test/shared", "on", kept=True)
    # Listed but its pattern does not admit it: under enforcement the next
    # pull would not touch it.
    _listed("https://jobs.test/screened", "on", kept=False)
    # Its own listing, which is no evidence: the source is off.
    _listed("https://jobs.test/by-off", "off", kept=True)

    assert catalog.retire_switched_off(patterns_enforced=True) == {"off": 3}

    assert not _active(alone) and not _active(screened) and not _active(by_off)
    assert _active(shared), "a url an active source admits is still listed"
    assert _active(on_job), "a switched-on source's rows are its own pull's business"
    assert _events(alone) == [False], "a retirement is an observation, like retire_unlisted's"
    assert _events(shared) == []

    # Idempotent: the next cycle has nothing to do.
    assert catalog.retire_switched_off(patterns_enforced=True) == {}


def test_without_enforcement_any_listing_by_an_active_source_keeps_the_row(f):
    f.make_source("off", active=False)
    f.make_source("on", active=True)
    screened = f.make_job(source="off", url="https://jobs.test/screened")
    _listed("https://jobs.test/screened", "on", kept=False)

    assert catalog.retire_switched_off(patterns_enforced=False) == {}
    assert _active(screened), "with enforcement off the pull admits every listed posting"


def test_re_enabled_source_pull_puts_its_postings_back(f, monkeypatch):
    """Retirement is reversible through the ordinary upsert, which logs the return."""
    from core.fetching import boards
    from core.fetching.posting import JobPosting

    f.make_source("off", active=False)
    job = f.make_job(source="off", url="https://jobs.test/back")
    catalog.retire_switched_off(patterns_enforced=True)
    assert not _active(job)

    db.execute("UPDATE sources SET active = true WHERE name = 'off'")
    post = JobPosting(
        company="Acme",
        locations=[],
        title="Engineer",
        url="https://jobs.test/back",
        terms=[],
        active=True,
        date_posted=0,
        raw_url="",
    )
    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: [post])
    asyncio.run(ingest.handle_ingest_source(f.make_task("ingest_source"), {"source": "off"}))
    assert _active(job)
    assert _events(job) == [False, True]


def test_the_task_retires_and_the_scheduler_queues_it_every_cycle(f):
    f.make_source("off", active=False)
    job = f.make_job(source="off")
    task_id = f.make_task("retire_switched_off")

    asyncio.run(ingest.handle_retire_switched_off(task_id, {}))

    assert not _active(job)
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["retired"] == 1

    worker.schedule_ingest_cycle()
    assert db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'retire_switched_off' AND dedupe_key IS NOT NULL"
    )
