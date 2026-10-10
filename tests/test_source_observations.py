"""catalog.observe: what each source said about each posting, as changes."""

from __future__ import annotations

import asyncio
import dataclasses

from api import db
from api.ai import verdicts
from core import catalog
from core.fetching import boards
from core.fetching.posting import JobPosting
from tasks import ingest

A = "https://jobs.test/a"
B = "https://jobs.test/b"


def _posting(url: str, title: str = "Engineer I", active: bool = True) -> JobPosting:
    return JobPosting(
        company="Acme",
        locations=["Remote"],
        title=title,
        url=url,
        terms=[],
        active=active,
        date_posted=1_700_000_000,
        raw_url="",
    )


def _history(source: str) -> list[tuple[str, str]]:
    return [
        (r["url"], r["kind"])
        for r in db.query(
            "SELECT j.url, o.kind FROM source_observations o JOIN jobs j ON j.id = o.job_id "
            "WHERE o.source = %s ORDER BY o.id",
            (source,),
        )
    ]


def _pull(source: str, postings: list[JobPosting], kept: set[str], absence: str | None):
    catalog.upsert_postings([p for p in postings if p.url in kept], source)
    return catalog.observe(source, None, postings, kept, absence)


def test_an_authoritative_board_records_each_change_once():
    a, b = _posting(A), _posting(B)
    assert _pull("board", [a, b], {A, B}, "unlisted") == {"appeared": 2}
    # The same pull again says nothing new.
    assert _pull("board", [a, b], {A, B}, "unlisted") == {}
    # Dropped from a complete pull, then listed again.
    assert _pull("board", [a], {A}, "unlisted") == {"unlisted": 1}
    assert _pull("board", [a], {A}, "unlisted") == {}
    assert _pull("board", [a, b], {A, B}, "unlisted") == {"reappeared": 1}
    assert _history("board") == [
        (A, "appeared"),
        (B, "appeared"),
        (B, "unlisted"),
        (B, "reappeared"),
    ]


def test_a_pattern_miss_is_filtered_never_unlisted():
    """Still listed, no longer admitted: a distinct observation, which is
    exactly what retire_unlisted cannot tell apart from a closure."""
    _pull("board", [_posting(A)], {A}, "unlisted")
    _pull("board", [_posting(A, "Senior Engineer")], set(), "unlisted")
    assert _history("board") == [(A, "appeared"), (A, "filtered")]


def test_absence_from_an_aggregator_is_not_listed_and_a_partial_pull_records_none():
    _pull("feed", [_posting(A), _posting(B)], {A, B}, "not_listed")
    # A pull that cannot show it saw everything records no absence at all.
    assert _pull("feed", [_posting(A)], {A}, None) == {}
    assert _pull("feed", [_posting(A)], {A}, "not_listed") == {"not_listed": 1}
    assert _history("feed")[-1] == (B, "not_listed")


def test_a_feed_flagging_a_posting_inactive_unlists_it():
    _pull("feed", [_posting(A)], {A}, "not_listed")
    _pull("feed", [_posting(A, active=False)], {A}, "not_listed")
    assert _history("feed") == [(A, "appeared"), (A, "unlisted")]


def test_each_source_observes_a_shared_posting_separately():
    """One url is one job; two sources listing it are two histories, and one
    dropping it says nothing about the other."""
    _pull("board", [_posting(A)], {A}, "unlisted")
    _pull("feed", [_posting(A)], {A}, "not_listed")
    _pull("board", [], set(), "unlisted")
    assert _history("board") == [(A, "appeared"), (A, "unlisted")]
    assert _history("feed") == [(A, "appeared")]
    assert db.query_one("SELECT count(*) AS n FROM jobs")["n"] == 1


def test_ingest_records_the_pull_under_its_task(monkeypatch, f):
    f.make_source("rocketlab")
    db.execute(
        "UPDATE sources SET listings_url = 'https://boards-api.greenhouse.io/v1/boards/x/jobs', "
        "title_pattern = 'engineer i' WHERE name = 'rocketlab'"
    )
    listed = [_posting(A), dataclasses.replace(_posting(B), title="Senior Engineer")]
    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: listed)

    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "rocketlab"})
    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "rocketlab"}))

    rows = db.query("SELECT kind, run_id FROM source_observations")
    # B has no catalog row (the pattern kept it out), so nothing points at it.
    assert rows == [{"kind": "appeared", "run_id": task_id}]
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["observed"] == {"appeared": 1}
    assert progress["complete"] is True


def _available(url: str, enforced: bool = True) -> bool | None:
    sql = catalog.AVAILABLE.format(job="j")
    row = db.query_one(
        f"SELECT {sql} AS a FROM jobs j WHERE j.url = %(url)s", {"url": url, "enforced": enforced}
    )
    return row["a"]


def test_availability_is_read_from_the_latest_observation_of_switched_on_sources(f):
    f.make_source("board")
    f.make_source("feed")
    f.make_source("off", active=False)
    _pull("board", [_posting(A)], {A}, "unlisted")
    assert _available(A) is True
    _pull("board", [], set(), "unlisted")
    assert _available(A) is False, "a complete authoritative pull left it out"
    # An aggregator that stops listing it keeps it available: absence there
    # says nothing about whether it closed.
    _pull("feed", [_posting(A)], {A}, "not_listed")
    _pull("feed", [], set(), "not_listed")
    assert _available(A) is True
    # Listed only by a switched-off source: not available.
    _pull("off", [_posting(B)], {B}, "unlisted")
    assert _available(B) is False
    # Filtered admits only while patterns are not enforced.
    _pull("board", [_posting(A, "Senior")], set(), "unlisted")
    db.execute("UPDATE sources SET active = false WHERE name = 'feed'")
    assert _available(A, enforced=True) is False
    assert _available(A, enforced=False) is True


def test_a_job_no_source_has_observed_is_cannot_tell(f):
    f.make_job(url=A)
    assert _available(A) is None


def test_the_shadow_counts_each_disagreement_once(f):
    f.make_source("board")
    _pull("board", [_posting(A), _posting(B)], {A, B}, "unlisted")
    # The legacy flag says closed where the observations say listed.
    db.execute("UPDATE jobs SET active = false WHERE url = %s", (B,))
    cells = ingest._summarise(catalog.availability_shadow(patterns_enforced=True))
    assert {k: v["n"] for k, v in cells.items()} == {
        "legacy=True projected=True": 1,
        "legacy=False projected=True": 1,
    }
    assert cells["legacy=False projected=True"]["sources"] == {"board": 1}
