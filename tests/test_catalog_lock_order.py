"""Catalog writes lock rows in one order, so two ingests cannot deadlock.

Boards share urls. 36 ingests failed on a deadlock in the 60 days to
2026-10-04, 32 inserting into listings, whose upsert ran in board order.

Each test reproduces the interleaving deterministically: a second
transaction holds the row that sorts first, the writer runs until it waits on
that lock, then the second transaction takes the row that sorts last. A
writer that locked in its own order already holds that row, and the deadlock
detector aborts one side. A writer that locks in url order holds nothing
while it waits.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import LiteralString

from api import db
from core import catalog
from core.fetching.posting import JobPosting
from core.pool import pool

FIRST = "https://jobs.test/a"
LAST = "https://jobs.test/z"


def _posting(url: str, title: str) -> JobPosting:
    return JobPosting(
        company="Acme",
        locations=["Remote"],
        title=title,
        url=url,
        terms=[],
        active=True,
        date_posted=1_700_000_000,
        raw_url="",
    )


def _contend(table: LiteralString, write: Callable[[], object]) -> None:
    failures: list[BaseException] = []

    def run() -> None:
        try:
            write()
        except BaseException as exc:
            failures.append(exc)

    lock: LiteralString = f"SELECT 1 FROM {table} WHERE url = %s FOR UPDATE"
    writer = threading.Thread(target=run)
    with pool.connection() as holder:
        holder.execute(lock, (FIRST,))
        writer.start()
        deadline = time.monotonic() + 10
        while not db.query_one(
            "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
            (holder.info.backend_pid,),
        ):
            assert writer.is_alive(), f"the writer finished without waiting: {failures}"
            assert time.monotonic() < deadline, "the writer never waited on the held row"
            time.sleep(0.02)
        holder.execute("SET LOCAL lock_timeout = '5s'")
        holder.execute(lock, (LAST,))
    writer.join(10)
    assert not writer.is_alive()
    assert not failures


def test_listings_upsert_locks_in_url_order():
    catalog.record_listings([_posting(LAST, "v1"), _posting(FIRST, "v1")], "board-a", "", set(), 24)
    # Board order, last url first: the order the upsert used to lock in.
    _contend(
        "listings",
        lambda: catalog.record_listings(
            [_posting(LAST, "v2"), _posting(FIRST, "v2")], "board-b", "", set(), 24
        ),
    )
    titles = {r["url"]: r["title"] for r in db.query("SELECT url, title FROM listings")}
    assert titles == {FIRST: "v2", LAST: "v2"}


def test_retiring_unlisted_jobs_locks_in_url_order():
    catalog.upsert_postings([_posting(LAST, "v1")], "board-a")
    catalog.upsert_postings([_posting(FIRST, "v1")], "board-a")
    _contend("jobs", lambda: catalog.retire_unlisted("board-a", []))
    assert db.query("SELECT url FROM jobs WHERE active") == []


def test_observing_locks_in_url_order():
    """An observation's foreign key share-locks its job row, which conflicts
    with retire_unlisted's FOR UPDATE."""
    catalog.upsert_postings([_posting(LAST, "v1")], "board-a")
    catalog.upsert_postings([_posting(FIRST, "v1")], "board-a")
    _contend(
        "jobs",
        lambda: catalog.observe(
            "board-a", None, [_posting(LAST, "v1"), _posting(FIRST, "v1")], {FIRST, LAST}, None
        ),
    )
    assert db.query_one("SELECT count(*) AS n FROM source_observations")["n"] == 2


def test_near_copy_keys_lock_in_url_order():
    catalog.upsert_postings([_posting(LAST, "v1")], "board-a")
    catalog.upsert_postings([_posting(FIRST, "v1")], "board-a")
    _contend("jobs", lambda: catalog.set_near_copy_keys({LAST: "twin", FIRST: "twin"}))
    assert {r["near_copy_key"] for r in db.query("SELECT near_copy_key FROM jobs")} == {"twin"}


def test_refreshing_stored_availability_locks_in_url_order():
    catalog.upsert_postings([_posting(LAST, "v1")], "board-a")
    catalog.upsert_postings([_posting(FIRST, "v1")], "board-a")
    ids = [r["id"] for r in db.query("SELECT id FROM jobs ORDER BY id DESC")]
    db.execute(
        "INSERT INTO source_observations (job_id, source, kind) "
        "SELECT unnest(%s::bigint[]), 'board-a', 'appeared'",
        (ids,),
    )
    # Ids in insertion order put LAST first, the order a scan meets them.
    _contend("jobs", lambda: catalog.refresh_available(ids))
    assert db.query_one("SELECT count(*) AS n FROM jobs WHERE available IS NOT NULL")["n"] == 2
