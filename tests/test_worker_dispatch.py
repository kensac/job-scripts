"""Worker handler dispatch, chunk reconciliation, and status reporting."""

from __future__ import annotations

import pytest

from api import ai, db, fetching, worker
from core import page_fetches
from core.store import add_ai_result
from tasks import content as tasks_content
from tasks import runtime as tasks_runtime
from tests.worker_queue_support import _clear_claim as _clear_claim

pytestmark = pytest.mark.usefixtures("_clear_claim")


@pytest.mark.asyncio
async def test_run_once_returns_false_on_empty_queue():
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_run_once_dispatches_to_registered_handler(monkeypatch):
    calls = []

    async def fake_handler(task_id, payload):
        calls.append((task_id, payload))

    monkeypatch.setitem(worker.HANDLERS, "test_kind", fake_handler)
    task_id = tasks_runtime.enqueue("test_kind", {"x": 1})
    assert await worker.run_once() is True
    assert calls == [(task_id, {"x": 1})]
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_run_once_marks_failed_when_handler_raises(monkeypatch):
    async def bad_handler(task_id, payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", bad_handler)
    task_id = tasks_runtime.enqueue("test_kind", {})
    await worker.run_once()
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed"
    assert "boom" in row["error"]


@pytest.mark.asyncio
async def test_run_once_leaves_an_unknown_kind_pending():
    task_id = tasks_runtime.enqueue("no_such_kind", {})
    assert await worker.run_once() is False
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "pending" and row["error"] is None


def test_reconcile_chunks_cancels_pending_chunk_of_cancelled_parent():
    parent_id = tasks_runtime.enqueue("run_all_filters", {"user_id": 999})
    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (parent_id,))
    chunk_id = tasks_runtime.enqueue(
        "run_filter_chunk",
        {"parent_id": parent_id, "user_id": 999, "filter": {}, "jobs": []},
    )
    worker.reconcile_chunks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (chunk_id,))
    assert row["status"] == "cancelled"


def test_reconcile_chunks_finalizes_waiting_parent_with_no_live_chunks(user_headers):
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    parent_id = tasks_runtime.enqueue("run_all_filters", {"user_id": user_id})
    db.execute("UPDATE tasks SET status = 'waiting' WHERE id = %s", (parent_id,))
    worker.reconcile_chunks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (parent_id,))
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_chunked_run_all_filters_lifecycle(set_config, monkeypatch, user_headers):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    set_config("filter_chunk_size", 5)

    async def no_network(url):
        raise AssertionError(f"scrape attempted for {url}, content should have been cached")

    monkeypatch.setattr(fetching, "fetch_page", no_network)

    async def fake_parse(cfg, instructions, input_text, response_model, timeout=120.0):
        should_filter = "REJECT_ME" in input_text
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return response_model(should_filter=should_filter), usage

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
            "INSERT INTO jobs (url, company, title, source, available) "
            "VALUES (%s, %s, 'SWE', 'internships', true)",
            (url, f"co{i}"),
        )
        content = "REJECT_ME content" if url in rejected_urls else "great job content"
        page_fetches.record(url, "passed", "scraped", content)
        add_ai_result(url, "passed", "not closed", "closed")

    parent_id = tasks_runtime.enqueue("run_all_filters", {"user_id": user_id})

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
            "SELECT j.url FROM user_job_working_set ws JOIN jobs j ON j.id = ws.job_id "
            "WHERE ws.user_id = %s",
            (user_id,),
        )
    }
    assert board_urls == set(urls) - rejected_urls

    # This previously guarded against unintentionally discarding explanations
    # when a filter emptied the board. Decision-only output is now explicit:
    # preserve missing evidence as NULL, with reasons available through Explain.
    custom_reasons = {
        r["reason"]
        for r in db.query(
            "SELECT reason FROM ai_queries WHERE check_type = 'custom' AND status = 'rejected'"
        )
    }
    assert custom_reasons == {None}


def test_worker_status_report_persists_current_claim_ids(monkeypatch):
    from api import worker

    worker._report_worker_status()
    from api import db

    row = db.query_one("SELECT * FROM worker_status WHERE name = %s", (worker.WORKER_NAME,))
    assert row is not None and row["current_task_id"] is None
    assert row["current_task_ids"] == []
    claims = {t: tasks_runtime.TaskClaim(t, worker.WORKER_NAME, 1) for t in (43, 42)}
    monkeypatch.setattr(worker, "_in_flight", claims)
    worker._report_worker_status()
    row = db.query_one(
        "SELECT current_task_id, current_task_ids FROM worker_status WHERE name = %s",
        (worker.WORKER_NAME,),
    )
    assert row["current_task_id"] == 42 and row["current_task_ids"] == [42, 43]


@pytest.mark.asyncio
async def test_transient_error_requeues_instead_of_failing(monkeypatch):
    async def oom_handler(task_id, payload):
        raise RuntimeError("can't start new thread")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", oom_handler)
    tid = tasks_runtime.enqueue("test_kind", {})
    assert await worker.run_once() is True
    row = db.query_one("SELECT status, attempts, error FROM tasks WHERE id = %s", (tid,))
    assert row["status"] == "pending" and "transient" in row["error"]

    # Past task_max_attempts it gives up rather than looping forever.
    db.execute(
        "UPDATE tasks SET attempts = %s WHERE id = %s", (db.get_config("task_max_attempts"), tid)
    )
    assert await worker.run_once() is True
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (tid,))
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_non_transient_error_still_fails_immediately(monkeypatch):
    async def bad_handler(task_id, payload):
        raise ValueError("genuinely broken")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", bad_handler)
    tid = tasks_runtime.enqueue("test_kind", {})
    assert await worker.run_once() is True
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (tid,))
    assert row["status"] == "failed" and "genuinely broken" in row["error"]


@pytest.mark.asyncio
async def test_content_backfill_caches_pages_and_skips_covered_jobs(monkeypatch):
    from core.fetching import ats as core_ats
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('bf', 'https://x') ON CONFLICT DO NOTHING"
    )
    db.execute(
        "INSERT INTO users (sub, email) VALUES ('bf-user', 'b@f.com') ON CONFLICT DO NOTHING"
    )
    uid = db.query_one("SELECT id FROM users WHERE sub = 'bf-user'")["id"]
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, 'bf') ON CONFLICT DO NOTHING",
        (uid,),
    )
    db.execute(
        "INSERT INTO jobs (url, source, company, title, available) VALUES "
        "('https://bf.test/needs', 'bf', 'A', 'T', true), ('https://bf.test/has', 'bf', 'B', 'T', true), "
        "('https://bf.test/gone', 'bf', 'C', 'T', true)"
    )
    # A posting its board reported gone has a closed verdict and no content
    # row; it was re-fetched every hour for two days before this line.
    add_ai_result("https://bf.test/gone", "rejected", "ATS reports posting gone", "closed")
    page_fetches.record("https://bf.test/has", "passed", "scraped", "X" * 500)

    scraped = []

    async def fake_fetch_page(url):
        scraped.append(url)
        return "Y" * 500, False

    monkeypatch.setattr(fetching, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(core_ats, "resolve", lambda url: core_ats.UNSUPPORTED)

    tid = tasks_runtime.enqueue("fetch_missing_content", {})
    await tasks_content.handle_fetch_missing_content(tid, {})

    assert scraped == ["https://bf.test/needs"]
    # Re-running finds nothing: the task is self-limiting once gaps are closed.
    scraped.clear()
    await tasks_content.handle_fetch_missing_content(tid, {})
    assert scraped == []


def test_worker_startup_seeds_open_alert_count_from_database():
    from api import metrics, worker

    db.execute(
        "INSERT INTO health_alerts (kind, subject, severity, message) "
        "VALUES ('ingest_failing', 'x', 'warning', 'm'), ('ingest_failing', 'y', 'warning', 'm')"
    )
    worker._seed_gauges()
    assert metrics.HEALTH_ALERTS._value.get() == 2
