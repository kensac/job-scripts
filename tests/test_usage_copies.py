"""Emptying the usage copies the call ledger replaced, ahead of their drop."""

import asyncio

from api import db
from tasks import usage_copies


def _seed(n: int) -> None:
    for i in range(n):
        db.execute(
            "INSERT INTO ai_batches (provider_batch_id, purpose, input_tokens, output_tokens, "
            "cache_write_tokens, est_cost_usd) VALUES (%s, 'comp', 10, 2, 1, 0.1)",
            (f"b{i}",),
        )
        db.execute(
            "INSERT INTO job_embeddings (url, embedding, model, content_hash, input_tokens, "
            "cost_usd) VALUES (%s, array_fill(0.1::real, ARRAY[1536])::vector, 'm', 'h', 5, 0.01)",
            (f"https://e.test/{i}",),
        )


def _run(f) -> dict:
    task_id = f.make_task("clear_usage_copies", status="running")
    asyncio.run(usage_copies.handle_clear_usage_copies(task_id, {}))
    return db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]


def test_every_copy_is_emptied_in_chunks_and_counted(f, monkeypatch):
    monkeypatch.setattr(usage_copies, "CHUNK", 2)
    _seed(5)
    progress = _run(f)
    assert progress["before"] == {"ai_batches": 5, "job_embeddings": 5}
    assert progress["cleared"] == {"ai_batches": 5, "job_embeddings": 5}
    assert progress["after"] == {"ai_batches": 0, "job_embeddings": 0}
    assert usage_copies.remaining() == {"ai_batches": 0, "job_embeddings": 0}
    assert db.query_one("SELECT count(*) AS n FROM job_embeddings")["n"] == 5, "rows stay"
    assert _run(f)["total"] == 0, "a second run finds nothing"


def test_a_new_row_takes_no_copy():
    db.execute("INSERT INTO ai_batches (provider_batch_id, purpose) VALUES ('new', 'comp')")
    db.execute(
        "INSERT INTO job_embeddings (url, embedding, model, content_hash) "
        "VALUES ('https://e.test/new', array_fill(0.1::real, ARRAY[1536])::vector, 'm', 'h')"
    )
    assert usage_copies.remaining() == {"ai_batches": 0, "job_embeddings": 0}


def test_the_worker_stops_queueing_once_a_run_found_nothing(f):
    from api import worker

    def queued() -> int:
        return db.query_one(
            "SELECT count(*) AS n FROM tasks WHERE kind = 'clear_usage_copies' AND status = 'pending'"
        )["n"]

    worker.schedule_ingest_cycle()
    assert queued() == 1
    db.execute(
        "UPDATE tasks SET status = 'done', progress = '{\"total\": 0}'::jsonb "
        "WHERE kind = 'clear_usage_copies'"
    )
    worker.schedule_ingest_cycle()
    assert queued() == 0
