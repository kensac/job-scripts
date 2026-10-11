"""A posting is never available only because of a source nobody pulls.

The hourly retire_switched_off task keeps its name and now stores
catalog.AVAILABLE (catalog.reconcile_available): a source switched off or on
changes availability without an observation, and lands here.
"""

from __future__ import annotations

import asyncio

from api import db, worker
from core import catalog
from tasks import ingest


def _observe(job_id: int, source: str, kind: str = "appeared") -> None:
    db.execute(
        "INSERT INTO source_observations (job_id, source, kind) VALUES (%s, %s, %s)",
        (job_id, source, kind),
    )


def _available(job_id: int) -> bool:
    return db.query_one(
        f"SELECT {catalog.IS_AVAILABLE.format(job='j')} AS a FROM jobs j WHERE j.id = %s",
        (job_id,),
    )["a"]


def test_switched_off_source_postings_are_unavailable_unless_a_switched_on_source_lists_them(f):
    f.make_source("off", active=False)
    f.make_source("on")
    alone = f.make_job(source="off")
    shared = f.make_job(source="off")
    _observe(alone, "off")
    _observe(shared, "off")
    _observe(shared, "on")

    catalog.reconcile_available()

    assert not _available(alone)
    assert _available(shared), "a switched-on source still lists it"


def test_re_enabling_a_source_makes_its_postings_available_again(f):
    f.make_source("flip", active=False)
    job = f.make_job(source="flip")
    _observe(job, "flip")
    catalog.reconcile_available()
    assert not _available(job)

    db.execute("UPDATE sources SET active = true WHERE name = 'flip'")
    catalog.reconcile_available()
    assert _available(job)


def test_the_task_reconciles_and_the_scheduler_queues_it_every_cycle(f):
    f.make_source("off", active=False)
    job = f.make_job(source="off")
    _observe(job, "off")
    task_id = f.make_task("retire_switched_off")

    asyncio.run(ingest.handle_retire_switched_off(task_id, {}))

    assert not _available(job)
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["available_reconciled"] == 1

    worker.schedule_ingest_cycle()
    assert db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'retire_switched_off' AND dedupe_key IS NOT NULL"
    )


def test_a_row_an_ingest_holds_is_skipped_and_stored_next_cycle(f):
    """It runs beside every ingest. Waiting on a held row is how two writers
    deadlock; skipping it costs one cycle."""
    from core.pool import pool

    f.make_source("off", active=False)
    held = f.make_job(source="off", url="https://jobs.test/held")
    free = f.make_job(source="off", url="https://jobs.test/free")
    _observe(held, "off")
    _observe(free, "off")
    with pool.connection() as holder:
        holder.execute("SELECT 1 FROM jobs WHERE id = %s FOR UPDATE", (held,))
        assert catalog.reconcile_available() == 1
    assert _available(held) and not _available(free)
    assert catalog.reconcile_available() == 1
    assert not _available(held)
