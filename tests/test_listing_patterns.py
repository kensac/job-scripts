"""listings.pattern is emptied: each row points at the stored copy of its
text instead, and a second run finds nothing left."""

from __future__ import annotations

import asyncio
import dataclasses

from api import db, worker
from core import catalog
from core.fetching.posting import JobPosting
from tasks import listing_patterns

KIND = "drop_listing_pattern_copies"


def _run(f) -> dict:
    task_id = f.make_task(KIND, status="running")
    asyncio.run(listing_patterns.handle_drop_listing_pattern_copies(task_id, {}))
    row = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))
    assert row is not None
    return row["progress"]


def test_every_copy_is_emptied_and_its_row_points_at_its_own_text(f):
    stale = catalog.title_pattern_id("new grad")
    # Rows from before pattern_id, more than one batch of one source, so a
    # source takes several passes.
    db.execute(
        "INSERT INTO listings (url, source, pattern) "
        "SELECT 'https://a.test/' || i, 'a', 'new grad' FROM generate_series(1, %s) i",
        (catalog._BATCH + 3,),
    )
    db.execute(
        "INSERT INTO listings (url, source, pattern) VALUES "
        "('https://b.test/1', 'b', 'intern'), ('https://b.test/2', 'b', '')"
    )
    # Pointed and still carrying the text, which a writer that did not know
    # pattern_id changed afterwards: the text is what its last writer meant.
    db.execute(
        "INSERT INTO listings (url, source, pattern, pattern_id) "
        "VALUES ('https://c.test/1', 'c', 'engineer', %s)",
        (stale,),
    )
    # Already moved; nothing to do.
    db.execute(
        "INSERT INTO listings (url, source, pattern_id) VALUES ('https://c.test/2', 'c', %s)",
        (stale,),
    )
    texts = {
        r["url"]: r["pattern"]
        for r in db.query("SELECT url, pattern FROM listings WHERE pattern IS NOT NULL")
    }

    n = catalog._BATCH + 6
    assert _run(f) == {
        "done": n,
        "total": n,
        "label": f"emptied {n} of {n} listings",
        "emptied": n,
    }
    assert db.query_one("SELECT count(*) AS n FROM listings WHERE pattern IS NOT NULL") == {"n": 0}
    pointed = {
        r["url"]: r["stored"]
        for r in db.query(
            "SELECT l.url, t.pattern AS stored FROM listings l "
            "JOIN title_patterns t ON t.id = l.pattern_id"
        )
    }
    assert len(pointed) == n + 1
    assert {url: pointed[url] for url in texts} == texts
    assert pointed["https://c.test/2"] == "new grad"

    assert _run(f)["total"] == 0


def test_a_pull_writes_no_copy_and_empties_the_row_it_rewrites(f):
    """Only the pointer is written. Rewriting a row that still carries the
    copy (here, at its refresh) empties it."""
    posting = JobPosting(
        company="Acme",
        locations=[],
        title="Engineer",
        url="https://a.test/new",
        terms=[],
        active=True,
        date_posted=None,
        raw_url="",
    )
    db.execute(
        "INSERT INTO listings (url, source, pattern, last_seen_at) "
        "VALUES ('https://a.test/old', 'a', 'x', now() - interval '2 days')"
    )
    old = dataclasses.replace(posting, url="https://a.test/old")
    catalog.record_listings([posting, old], "a", "x", set(), 30, 24)
    rows = db.query(
        "SELECT l.pattern, t.pattern AS stored FROM listings l "
        "JOIN title_patterns t ON t.id = l.pattern_id"
    )
    assert rows == [{"pattern": None, "stored": "x"}] * 2


def test_the_scheduler_queues_one_run_until_one_starts_with_nothing_left(f):
    worker.schedule_ingest_cycle()
    db.execute(
        "UPDATE tasks SET dedupe_key = 'listing-pattern-copies:an-earlier-cycle' WHERE kind = %s",
        (KIND,),
    )
    worker.schedule_ingest_cycle()
    assert db.query("SELECT status FROM tasks WHERE kind = %s", (KIND,)) == [{"status": "pending"}]

    db.execute(
        "UPDATE tasks SET status = 'done', progress = '{\"done\": 3, \"total\": 3}' "
        "WHERE kind = %s",
        (KIND,),
    )
    worker.schedule_ingest_cycle()
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = %s", (KIND,)) == {"n": 2}, (
        "a run that found rows is followed by another"
    )

    db.execute(
        "UPDATE tasks SET status = 'done', progress = '{\"done\": 0, \"total\": 0}', "
        "dedupe_key = NULL WHERE kind = %s",
        (KIND,),
    )
    worker.schedule_ingest_cycle()
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = %s", (KIND,)) == {"n": 2}, (
        "a run that started with nothing left ends the schedule"
    )
