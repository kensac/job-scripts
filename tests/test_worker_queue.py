from __future__ import annotations

import pytest

from api import ai, db, verdicts, worker
from core.store import add_ai_result

# ---------------------------------------------------------------------------
# enqueue / dedupe
# ---------------------------------------------------------------------------


def test_enqueue_dedupe_key_second_call_returns_none():
    first = worker.enqueue("run_filter", {"x": 1}, dedupe_key="dk1")
    second = worker.enqueue("run_filter", {"x": 2}, dedupe_key="dk1")
    assert first is not None
    assert second is None
    rows = db.query("SELECT id FROM tasks WHERE dedupe_key = %s", ("dk1",))
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# _claim_task
# ---------------------------------------------------------------------------


def test_claim_task_returns_none_on_empty_queue():
    assert worker._claim_task() is None


def test_claim_task_flips_to_running_and_stamps_worker():
    task_id = worker.enqueue("run_filter", {"a": 1})
    claimed = worker._claim_task()
    assert claimed["id"] == task_id
    row = db.query_one(
        "SELECT status, attempts, worker, last_heartbeat FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert row["status"] == "running"
    assert row["attempts"] == 1
    assert row["worker"] == worker.WORKER_NAME
    assert row["last_heartbeat"] is not None


def test_claim_task_claims_in_id_order():
    first_id = worker.enqueue("run_filter", {"a": 1})
    second_id = worker.enqueue("run_filter", {"a": 2})
    assert worker._claim_task()["id"] == first_id
    assert worker._claim_task()["id"] == second_id


# ---------------------------------------------------------------------------
# reap_stale_tasks
# ---------------------------------------------------------------------------


def test_reap_stale_tasks_requeues_below_max_attempts():
    task_id = worker.enqueue("run_filter", {})
    worker._claim_task()
    db.execute(
        "UPDATE tasks SET last_heartbeat = now() - interval '20 minutes' WHERE id = %s",
        (task_id,),
    )
    worker.reap_stale_tasks()
    row = db.query_one(
        "SELECT status, attempts, started_at, last_heartbeat FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["started_at"] is None
    assert row["last_heartbeat"] is None


def test_reap_stale_tasks_fails_after_max_attempts():
    task_id = worker.enqueue("run_filter", {})
    db.execute(
        "UPDATE tasks SET status = 'running', attempts = 3, "
        "last_heartbeat = now() - interval '20 minutes' WHERE id = %s",
        (task_id,),
    )
    worker.reap_stale_tasks()
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed"
    assert "worker lost" in row["error"]


def test_reap_stale_tasks_leaves_fresh_heartbeat_running():
    task_id = worker.enqueue("run_filter", {})
    worker._claim_task()
    worker.reap_stale_tasks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running"


# ---------------------------------------------------------------------------
# _finish
# ---------------------------------------------------------------------------


def test_finish_sets_status_when_running():
    task_id = worker.enqueue("run_filter", {})
    worker._claim_task()
    worker._finish(task_id, "done")
    row = db.query_one("SELECT status, finished_at FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"
    assert row["finished_at"] is not None


def test_finish_is_noop_on_cancelled_task():
    task_id = worker.enqueue("run_filter", {})
    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (task_id,))
    worker._finish(task_id, "done")
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# _graceful_exit's requeue SQL (can't call the handler itself, it os._exit()s)
# ---------------------------------------------------------------------------

_REQUEUE_SQL = (
    "UPDATE tasks SET status = 'pending', attempts = GREATEST(attempts - 1, 0), "
    "started_at = NULL, last_heartbeat = NULL "
    "WHERE id = %s AND status = 'running'"
)


def test_graceful_exit_requeue_sql_decrements_attempts_on_running_task():
    task_id = worker.enqueue("run_filter", {})
    worker._claim_task()
    db.execute(_REQUEUE_SQL, (task_id,))
    row = db.query_one(
        "SELECT status, attempts, started_at, last_heartbeat FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["started_at"] is None
    assert row["last_heartbeat"] is None


def test_graceful_exit_requeue_sql_noop_on_done_task():
    task_id = worker.enqueue("run_filter", {})
    worker._claim_task()
    worker._finish(task_id, "done")
    db.execute(_REQUEUE_SQL, (task_id,))
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# run_once dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_returns_false_on_empty_queue():
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_run_once_dispatches_to_registered_handler(monkeypatch):
    calls = []

    async def fake_handler(task_id, payload):
        calls.append((task_id, payload))

    monkeypatch.setitem(worker.HANDLERS, "test_kind", fake_handler)
    task_id = worker.enqueue("test_kind", {"x": 1})
    assert await worker.run_once() is True
    assert calls == [(task_id, {"x": 1})]
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_run_once_marks_failed_when_handler_raises(monkeypatch):
    async def bad_handler(task_id, payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", bad_handler)
    task_id = worker.enqueue("test_kind", {})
    await worker.run_once()
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed"
    assert "boom" in row["error"]


@pytest.mark.asyncio
async def test_run_once_unknown_kind_fails_with_message():
    task_id = worker.enqueue("no_such_kind", {})
    await worker.run_once()
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed"
    assert "unknown task kind" in row["error"]


# ---------------------------------------------------------------------------
# _reconcile_chunks
# ---------------------------------------------------------------------------


def test_reconcile_chunks_cancels_pending_chunk_of_cancelled_parent():
    parent_id = worker.enqueue("run_all_filters", {"user_id": 999})
    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (parent_id,))
    chunk_id = worker.enqueue(
        "run_filter_chunk",
        {"parent_id": parent_id, "user_id": 999, "filter": {}, "jobs": []},
    )
    worker._reconcile_chunks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (chunk_id,))
    assert row["status"] == "cancelled"


def test_reconcile_chunks_finalizes_waiting_parent_with_no_live_chunks(user_headers):
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    parent_id = worker.enqueue("run_all_filters", {"user_id": user_id})
    db.execute("UPDATE tasks SET status = 'waiting' WHERE id = %s", (parent_id,))
    worker._reconcile_chunks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (parent_id,))
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Chunked run_all_filters lifecycle, end to end through run_once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunked_run_all_filters_lifecycle(monkeypatch, user_headers):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(worker, "CHUNK_SIZE", 5)

    async def no_network(url):
        raise AssertionError(f"scrape attempted for {url}, content should have been cached")

    monkeypatch.setattr(worker, "_fetch_page", no_network)

    async def fake_parse(cfg, instructions, input_text, response_model, timeout=120.0):
        should_filter = "REJECT_ME" in input_text
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return response_model(should_filter=should_filter, reason="test"), usage

    monkeypatch.setattr(ai, "parse", fake_parse)

    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    db.execute("INSERT INTO user_sources (user_id, source) VALUES (%s, 'internships')", (user_id,))
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) VALUES (%s, %s, %s, %s)",
        (user_id, "f1", "no crypto companies", "hash1"),
    )

    urls = [f"https://jobs.example.com/job-{i}" for i in range(12)]
    rejected_urls = set(urls[:4])
    for i, url in enumerate(urls):
        db.execute(
            "INSERT INTO jobs (url, company, title, source) VALUES (%s, %s, 'SWE', 'internships')",
            (url, f"co{i}"),
        )
        content = "REJECT_ME content" if url in rejected_urls else "great job content"
        add_ai_result(url, "passed", "content cached", "content", input_content=content)
        add_ai_result(url, "passed", "not closed", "closed")

    parent_id = worker.enqueue("run_all_filters", {"user_id": user_id})

    assert await worker.run_once() is True
    parent = db.query_one("SELECT status FROM tasks WHERE id = %s", (parent_id,))
    assert parent["status"] == "waiting"
    chunks = db.query(
        "SELECT id, status FROM tasks WHERE kind = 'run_filter_chunk' "
        "AND (payload->>'parent_id')::bigint = %s",
        (parent_id,),
    )
    assert len(chunks) == 3

    while await worker.run_once():
        pass

    parent = db.query_one("SELECT status FROM tasks WHERE id = %s", (parent_id,))
    assert parent["status"] == "done"
    chunks = db.query(
        "SELECT status FROM tasks WHERE kind = 'run_filter_chunk' "
        "AND (payload->>'parent_id')::bigint = %s",
        (parent_id,),
    )
    assert all(c["status"] == "done" for c in chunks)

    board_urls = {
        r["url"]
        for r in db.query(
            "SELECT j.url FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id WHERE uj.user_id = %s",
            (user_id,),
        )
    }
    assert board_urls == set(urls) - rejected_urls


# ---------------------------------------------------------------------------
# _reverify_jobs resumability: fresh verdicts are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverify_jobs_skips_urls_with_fresh_closed_verdicts(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fetch_calls = []
    check_calls = []

    async def fake_fetch_page(url):
        fetch_calls.append(url)
        return "some job content"

    async def fake_run_check(cfg, **kwargs):
        check_calls.append(kwargs["url"])
        return None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    monkeypatch.setattr(worker, "_fetch_page", fake_fetch_page)
    monkeypatch.setattr(verdicts, "run_check", fake_run_check)

    fresh_urls = ["https://x.example.com/fresh-1", "https://x.example.com/fresh-2"]
    stale_urls = ["https://x.example.com/stale-1", "https://x.example.com/stale-2"]
    for url in fresh_urls:
        add_ai_result(url, "passed", "still open", "closed")

    rows = [{"url": u, "company": "Acme", "title": "SWE"} for u in fresh_urls + stale_urls]

    task_id = worker.enqueue("reverify_open", {})
    worker._claim_task()
    await worker._reverify_jobs(task_id, rows)

    assert sorted(check_calls) == sorted(stale_urls)
    assert sorted(fetch_calls) == sorted(stale_urls)
