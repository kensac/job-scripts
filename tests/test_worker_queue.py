from __future__ import annotations

import pytest

from api import ai, db, fetching, worker
from api.tasks import content as tasks_content
from api.tasks import filters as tasks_filters
from api.tasks import runtime as tasks_runtime
from api.tasks import verify as tasks_verify
from core.store import add_ai_result

# ---------------------------------------------------------------------------
# enqueue / dedupe
# ---------------------------------------------------------------------------


def test_enqueue_dedupe_key_second_call_returns_none():
    first = tasks_runtime.enqueue("run_filter", {"x": 1}, dedupe_key="dk1")
    second = tasks_runtime.enqueue("run_filter", {"x": 2}, dedupe_key="dk1")
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
    task_id = tasks_runtime.enqueue("run_filter", {"a": 1})
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
    first_id = tasks_runtime.enqueue("run_filter", {"a": 1})
    second_id = tasks_runtime.enqueue("run_filter", {"a": 2})
    assert worker._claim_task()["id"] == first_id
    assert worker._claim_task()["id"] == second_id


# ---------------------------------------------------------------------------
# reap_stale_tasks
# ---------------------------------------------------------------------------


def test_reap_stale_tasks_requeues_below_max_attempts():
    task_id = tasks_runtime.enqueue("run_filter", {})
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
    task_id = tasks_runtime.enqueue("run_filter", {})
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
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    worker.reap_stale_tasks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running"


# ---------------------------------------------------------------------------
# _finish
# ---------------------------------------------------------------------------


def test_finish_sets_status_when_running():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    tasks_runtime._finish(task_id, "done")
    row = db.query_one("SELECT status, finished_at FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"
    assert row["finished_at"] is not None


def test_finish_is_noop_on_cancelled_task():
    task_id = tasks_runtime.enqueue("run_filter", {})
    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (task_id,))
    tasks_runtime._finish(task_id, "done")
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# _graceful_exit's requeue SQL (can't call the handler itself, it os._exit()s)
# ---------------------------------------------------------------------------

# The statement itself, not a copy of it: editing the requeue in worker.py has
# to move these tests or break them.
_REQUEUE_SQL = worker._REQUEUE_ON_EXIT_SQL


def test_graceful_exit_requeue_sql_decrements_attempts_on_running_task():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    db.execute(_REQUEUE_SQL, (task_id, worker.WORKER_NAME, 1))
    row = db.query_one(
        "SELECT status, attempts, started_at, last_heartbeat FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["started_at"] is None
    assert row["last_heartbeat"] is None


def test_graceful_exit_requeue_sql_noop_on_done_task():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    tasks_runtime._finish(task_id, "done")
    db.execute(_REQUEUE_SQL, (task_id, worker.WORKER_NAME, 1))
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"


def test_graceful_exit_requeue_sql_noop_once_the_claim_is_gone():
    """A deploy is when a worker is most likely to have already lost its task:
    SIGTERM must not requeue a run another worker is midway through."""
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _reclaim_elsewhere(task_id)
    db.execute(_REQUEUE_SQL, (task_id, worker.WORKER_NAME, 1))
    row = db.query_one("SELECT status, attempts, worker FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert row["worker"] == "other-host"


# ---------------------------------------------------------------------------
# claim ownership: a worker may only write to the run it still holds
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_claim():
    """The claim is a contextvar the worker loop owns; a test that sets one by
    hand must not leak it into the next test."""
    yield
    tasks_runtime.set_current_claim(None)


def _hold_claim(task_id: int, attempts: int = 1) -> None:
    tasks_runtime.set_current_claim(tasks_runtime.TaskClaim(task_id, worker.WORKER_NAME, attempts))


def _reclaim_elsewhere(task_id: int) -> None:
    """What the reaper plus another host do to a task whose worker went stale:
    back to 'pending', then claimed again - which is the state the lost worker
    cannot distinguish from its own."""
    db.execute(
        "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL "
        "WHERE id = %s",
        (task_id,),
    )
    db.execute(
        "UPDATE tasks SET status = 'running', worker = 'other-host', "
        "attempts = attempts + 1, started_at = now(), last_heartbeat = now() "
        "WHERE id = %s",
        (task_id,),
    )


def test_park_refuses_once_the_task_has_been_reclaimed():
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    _reclaim_elsewhere(task_id)

    assert tasks_runtime._park_awaiting_batch(task_id, ["batch_lost"]) is False
    row = db.query_one("SELECT status, worker FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running", "must not park a run another worker holds"
    assert row["worker"] == "other-host"
    # The ids are still recorded: they are paid work, and dropping them would
    # leave nothing pointing at the batches.
    assert tasks_runtime._pending_batch_ids(task_id) == ["batch_lost"]


def test_park_succeeds_while_the_claim_is_held():
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)

    assert tasks_runtime._park_awaiting_batch(task_id, ["batch_a"]) is True
    row = db.query_one(
        "SELECT status, started_at, last_heartbeat FROM tasks WHERE id = %s", (task_id,)
    )
    assert row["status"] == "awaiting_batch"
    assert row["started_at"] is None and row["last_heartbeat"] is None


def test_park_does_not_duplicate_ids_the_hook_already_recorded():
    """A two-wave submit records each id through the hook, then parks with the
    full list; collection walks that list, so a duplicate is a second download
    of the same output file."""
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    hook = tasks_runtime._batch_event_hook(task_id, "filter", "gpt-5-nano")
    hook("batch_1", "validating", {"requests": 1, "completed": 0, "failed": 0})
    hook("batch_2", "validating", {"requests": 1, "completed": 0, "failed": 0})

    assert tasks_runtime._park_awaiting_batch(task_id, ["batch_1", "batch_2"]) is True
    assert tasks_runtime._pending_batch_ids(task_id) == ["batch_1", "batch_2"]


def test_park_records_ids_the_hook_never_saw():
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)

    assert tasks_runtime._park_awaiting_batch(task_id, ["batch_1", "batch_2"]) is True
    assert tasks_runtime._pending_batch_ids(task_id) == ["batch_1", "batch_2"]


def test_finish_refuses_once_the_task_has_been_reclaimed():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _hold_claim(task_id)
    _reclaim_elsewhere(task_id)

    tasks_runtime._finish(task_id, "done")
    row = db.query_one("SELECT status, finished_at FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running"
    assert row["finished_at"] is None


def test_finish_clears_batch_ids_so_a_rerun_cannot_recollect():
    """Verdicts are derived from scraped text captured before the batch was
    submitted. A finished task that kept its ids would collect them again on
    any later re-run and write verdicts from text that may predate a closure.
    """
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    hook = tasks_runtime._batch_event_hook(task_id, "filter", "gpt-5-nano")
    hook("batch_spent", "completed", {"requests": 1, "completed": 1, "failed": 0})
    assert tasks_runtime._pending_batch_ids(task_id) == ["batch_spent"]

    tasks_runtime._finish(task_id, "done")
    assert tasks_runtime._pending_batch_ids(task_id) == []
    row = db.query_one("SELECT status, payload FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"
    assert row["payload"]["parent_id"] == 1, "only batch_ids is dropped"


def test_transient_requeue_keeps_batch_ids_for_reattach():
    """The retry path is the reattach path: dropping the ids here would pay for
    the same batches twice."""
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    hook = tasks_runtime._batch_event_hook(task_id, "filter", "gpt-5-nano")
    hook("batch_live", "in_progress", {"requests": 1, "completed": 0, "failed": 0})

    db.execute(
        "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL "
        "WHERE id = %s AND status = 'running'",
        (task_id,),
    )
    assert tasks_runtime._pending_batch_ids(task_id) == ["batch_live"]


def test_progress_heartbeat_refuses_once_the_task_has_been_reclaimed():
    """_set_progress carries the heartbeat, so a lost worker would otherwise
    keep vouching for the liveness of the run that replaced it."""
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _hold_claim(task_id)
    _reclaim_elsewhere(task_id)
    db.execute("UPDATE tasks SET last_heartbeat = NULL WHERE id = %s", (task_id,))

    tasks_runtime._set_progress(task_id, 5, 10, "half way")
    row = db.query_one("SELECT last_heartbeat, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["last_heartbeat"] is None
    assert row["progress"] is None


def test_progress_writes_while_the_claim_is_held():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _hold_claim(task_id)
    tasks_runtime._set_progress(task_id, 5, 10, "half way")
    row = db.query_one("SELECT last_heartbeat, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["last_heartbeat"] is not None
    assert row["progress"]["done"] == 5 and row["progress"]["label"] == "half way"


def test_lifecycle_writes_are_unrestricted_without_a_claim():
    """Nothing outside the worker loop claims tasks, so a direct call keeps the
    behaviour it had before ownership was enforced."""
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    tasks_runtime._set_progress(task_id, 1, 2, "direct")
    tasks_runtime._finish(task_id, "done")
    row = db.query_one("SELECT status, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done" and row["progress"]["done"] == 1


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
async def test_run_once_unknown_kind_fails_with_message():
    task_id = tasks_runtime.enqueue("no_such_kind", {})
    await worker.run_once()
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed"
    assert "unknown task kind" in row["error"]


# ---------------------------------------------------------------------------
# _reconcile_chunks
# ---------------------------------------------------------------------------


def test_reconcile_chunks_cancels_pending_chunk_of_cancelled_parent():
    parent_id = tasks_runtime.enqueue("run_all_filters", {"user_id": 999})
    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (parent_id,))
    chunk_id = tasks_runtime.enqueue(
        "run_filter_chunk",
        {"parent_id": parent_id, "user_id": 999, "filter": {}, "jobs": []},
    )
    worker._reconcile_chunks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (chunk_id,))
    assert row["status"] == "cancelled"


def test_reconcile_chunks_finalizes_waiting_parent_with_no_live_chunks(user_headers):
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    parent_id = tasks_runtime.enqueue("run_all_filters", {"user_id": user_id})
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
    monkeypatch.setattr(tasks_filters, "CHUNK_SIZE", 5)

    async def no_network(url):
        raise AssertionError(f"scrape attempted for {url}, content should have been cached")

    monkeypatch.setattr(fetching, "fetch_page", no_network)

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
            "SELECT j.url FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id WHERE uj.user_id = %s",
            (user_id,),
        )
    }
    assert board_urls == set(urls) - rejected_urls


# ---------------------------------------------------------------------------
# _reverify_jobs resumability: fresh verdicts are skipped
# ---------------------------------------------------------------------------


async def _submit_ids(specs, model, effort, max_out, on_event=None):
    """Stands in for the provider accepting a submission."""
    _submit_ids.last_specs = list(specs)
    _submit_ids.calls = getattr(_submit_ids, "calls", 0) + 1
    return ["batch_test_1"]


def _collect_from(fake_batch):
    """Turns a test's existing result-builder into a collector, so the
    per-test expectations stay exactly as they were written."""

    async def _collect(batch_ids, on_event=None):
        return await fake_batch(getattr(_submit_ids, "last_specs", []), "m", "low", 0, None)

    return _collect


def _claim_for_test(task_id: int) -> int:
    """What _claim_task does to one specific row, plus the claim the loop holds
    for the duration of the handler."""
    row = db.query_one(
        "UPDATE tasks SET status = 'running', started_at = now(), last_heartbeat = now(), "
        "attempts = attempts + 1, worker = %s WHERE id = %s RETURNING attempts",
        (worker.WORKER_NAME, task_id),
    )
    assert row is not None
    _hold_claim(task_id, row["attempts"])
    return row["attempts"]


async def _run_batched(task_id, coro_factory):
    """Runs a batched handler through claim -> submit -> park -> resume -> collect.

    The first call submits and raises AwaitingBatch (freeing the worker in
    production); poll_batches then resumes the task, another claim picks it up
    and the second call finds the batch ids in the task payload and collects.
    Driving every hop is what makes these tests exercise the real lifecycle
    rather than a submit-and-wait that no longer exists - parking in particular
    is a write to a task the worker owns, so a handler that never claimed one
    is not a state the loop can reach.
    """
    from api.worker import AwaitingBatch

    _claim_for_test(task_id)
    try:
        await coro_factory()
    except AwaitingBatch:
        tasks_runtime._resume_parked(task_id)
        _claim_for_test(task_id)
        await coro_factory()


@pytest.mark.asyncio
async def test_reverify_jobs_skips_urls_with_fresh_closed_verdicts(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fetch_calls = []
    check_calls = []

    async def fake_fetch_page(url):
        fetch_calls.append(url)
        return "some job content", False

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        from core import batch as core_batch

        check_calls.extend(s.custom_id for s in specs)
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text='{"is_closed": false}',
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
            for s in specs
        }

    monkeypatch.setattr(fetching, "fetch_page", fake_fetch_page)
    monkeypatch.setattr("core.batch.submit_responses_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_batches", _collect_from(fake_batch))

    fresh_urls = ["https://x.example.com/fresh-1", "https://x.example.com/fresh-2"]
    stale_urls = ["https://x.example.com/stale-1", "https://x.example.com/stale-2"]
    for url in fresh_urls:
        add_ai_result(url, "passed", "still open", "closed")

    rows = [{"url": u, "company": "Acme", "title": "SWE"} for u in fresh_urls + stale_urls]

    task_id = tasks_runtime.enqueue("reverify_open", {})
    worker._claim_task()
    await _run_batched(task_id, lambda: tasks_verify._reverify_jobs(task_id, rows))

    assert sorted(check_calls) == sorted(stale_urls)
    assert sorted(fetch_calls) == sorted(stale_urls)


def test_batch_event_hook_registers_and_stores_ids():

    t1 = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1, "user_id": 1})
    hook = tasks_runtime._batch_event_hook(t1, "filter", "gpt-5-nano")
    hook("batch_abc", "validating", {"requests": 10, "completed": 0, "failed": 0})
    hook("batch_abc", "in_progress", {"requests": 10, "completed": 4, "failed": 0})
    hook("batch_abc", "completed", {"requests": 10, "completed": 9, "failed": 1})
    from api import db

    row = db.query_one("SELECT * FROM ai_batches WHERE provider_batch_id = 'batch_abc'")
    assert row["status"] == "completed" and row["completed"] == 9
    assert row["failed_count"] == 1 and row["completed_at"] is not None
    assert tasks_runtime._pending_batch_ids(t1) == ["batch_abc"]
    hook("batch_def", "validating", {"requests": 5, "completed": 0, "failed": 0})
    assert tasks_runtime._pending_batch_ids(t1) == ["batch_abc", "batch_def"]
    hook("batch_abc", "completed", {"requests": 10, "completed": 9, "failed": 1})
    assert tasks_runtime._pending_batch_ids(t1) == ["batch_abc", "batch_def"]


def test_worker_status_report_upserts():
    from api import worker

    worker._report_worker_status(None)
    from api import db

    row = db.query_one("SELECT * FROM worker_status WHERE name = %s", (worker.WORKER_NAME,))
    assert row is not None and row["current_task_id"] is None
    worker._report_worker_status(42)
    row = db.query_one(
        "SELECT current_task_id FROM worker_status WHERE name = %s", (worker.WORKER_NAME,)
    )
    assert row["current_task_id"] == 42


@pytest.mark.asyncio
async def test_verify_new_records_both_verdicts(monkeypatch):
    from api import db
    from core import batch as core_batch
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://v.test/1', 's', 'Acme', 'SWE')"
    )
    add_ai_result(
        "https://v.test/1", "passed", "content cached", "content", input_content="J" * 500
    )

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        assert effort == "low"
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text='{"is_closed": false, "requires_clearance_or_restrictions": true}',
                usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            )
            for s in specs
        }

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    monkeypatch.setattr("core.batch.submit_responses_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_batches", _collect_from(fake_batch))
    tid = tasks_runtime.enqueue("verify_new", {"cycle": "t"})
    await _run_batched(tid, lambda: tasks_verify.handle_verify_new(tid, {"cycle": "t"}))
    rows = db.query(
        "SELECT check_type, status FROM ai_queries WHERE url = 'https://v.test/1' "
        "AND check_type IN ('closed','clearance') ORDER BY check_type"
    )
    assert [(r["check_type"], r["status"]) for r in rows] == [
        ("clearance", "rejected"),
        ("closed", "passed"),
    ]


@pytest.mark.asyncio
async def test_reverify_records_batch_verdicts_and_reattaches(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from core import batch as core_batch

    submits = []

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        submits.append(len(specs))
        if on_event:
            on_event(
                "batch_rv1", "in_progress", {"requests": len(specs), "completed": 0, "failed": 0}
            )
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text='{"is_closed": true}',
                usage={"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
            )
            for s in specs
        }

    async def fake_collect(batch_ids, on_event=None):
        return {
            "https://rv.example.com/1": core_batch.BatchResult(
                "https://rv.example.com/1",
                text='{"is_closed": true}',
                usage={"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
            )
        }

    async def fake_fetch_page(url):
        return "job content here", False

    monkeypatch.setattr(fetching, "fetch_page", fake_fetch_page)
    monkeypatch.setattr("core.batch.submit_responses_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_batches", _collect_from(fake_batch))
    monkeypatch.setattr("core.batch.collect_batches", fake_collect)

    rows = [{"url": "https://rv.example.com/1", "company": "Acme", "title": "SWE"}]
    task_id = tasks_runtime.enqueue("reverify_chunk", {"parent_id": 1})
    worker._claim_task()
    await _run_batched(task_id, lambda: tasks_verify._reverify_jobs(task_id, rows))

    row = db.query_one(
        "SELECT status, config_name FROM ai_queries WHERE url = 'https://rv.example.com/1' "
        "AND check_type = 'closed' ORDER BY id DESC LIMIT 1"
    )
    assert row["status"] == "rejected" and row["config_name"] == "reverify"
    # Parked with its batch id recorded, so the work is recoverable.
    assert tasks_runtime._pending_batch_ids(task_id) == ["batch_test_1"]
    submitted_once = _submit_ids.calls

    # Requeued: the stored batch id makes it reattach, never resubmitting.
    await _run_batched(task_id, lambda: tasks_verify._reverify_jobs(task_id, rows))
    assert _submit_ids.calls == submitted_once, "resume must not resubmit"


@pytest.mark.asyncio
async def test_transient_error_requeues_instead_of_failing(monkeypatch):
    async def oom_handler(task_id, payload):
        raise RuntimeError("can't start new thread")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", oom_handler)
    tid = tasks_runtime.enqueue("test_kind", {})
    assert await worker.run_once() is True
    row = db.query_one("SELECT status, attempts, error FROM tasks WHERE id = %s", (tid,))
    assert row["status"] == "pending" and "transient" in row["error"]

    # Past MAX_ATTEMPTS it gives up rather than looping forever.
    db.execute("UPDATE tasks SET attempts = %s WHERE id = %s", (tasks_runtime.MAX_ATTEMPTS, tid))
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
    from core import ats as core_ats
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
        "INSERT INTO jobs (url, source, company, title) VALUES "
        "('https://bf.test/needs', 'bf', 'A', 'T'), ('https://bf.test/has', 'bf', 'B', 'T')"
    )
    add_ai_result(
        "https://bf.test/has",
        "passed",
        "content cached",
        "content",
        input_content="X" * 500,
        config_name="content-cache",
    )

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


@pytest.mark.asyncio
async def test_full_sweep_rechecks_even_fresh_verdicts(monkeypatch):
    """A forced sweep must overturn verdicts made today — skipping them is
    exactly what would preserve the stale-evidence verdicts it exists to fix."""
    from core import ats as core_ats
    from core import batch as core_batch
    from core.store import add_ai_result

    checked = []

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        checked.extend(s.custom_id for s in specs)
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text='{"is_closed": false}',
                usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
            )
            for s in specs
        }

    async def fake_fetch(url):
        return "fresh page text " * 40, False

    monkeypatch.setattr(fetching, "fetch_page", fake_fetch)
    monkeypatch.setattr(core_ats, "resolve", lambda url: core_ats.UNSUPPORTED)
    monkeypatch.setattr("core.batch.submit_responses_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_batches", _collect_from(fake_batch))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    url = "https://fs.test/1"
    add_ai_result(url, "passed", "job open", "closed")  # verdict from today
    rows = [{"url": url, "company": "C", "title": "T"}]

    tid = tasks_runtime.enqueue("reverify_chunk", {"parent_id": 1})
    await _run_batched(tid, lambda: tasks_verify._reverify_jobs(tid, rows))
    assert checked == [], "normal sweep should skip a verdict made today"

    await _run_batched(tid, lambda: tasks_verify._reverify_jobs(tid, rows, force=True))
    assert checked == [url], "forced sweep must re-check it anyway"


# ---------------------------------------------------------------------------
# reverify: evidence in a parked batch is older than the batch's own results
# ---------------------------------------------------------------------------


def _submitted_batch(provider_batch_id: str, minutes_ago: int) -> None:
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, submitted_at) "
        "VALUES (%s, 'reverify', now() - make_interval(mins => %s))",
        (provider_batch_id, minutes_ago),
    )


def _reverify_result(url: str, batch_id: str, is_closed: bool):
    from core.batch import BatchResult

    return BatchResult(
        url,
        text=f'{{"is_closed": {str(is_closed).lower()}}}',
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        batch_id=batch_id,
    )


def test_reverify_does_not_overturn_a_closure_settled_while_it_was_parked():
    """A parked batch carries page text as old as its submission. If something
    closed the job in the meantime, writing our result last would overturn a
    fresh closure with a stale page - and latest-row-wins would put the dead
    posting back on people's boards."""
    url = "https://stale.test/1"
    _submitted_batch("batch_parked", minutes_ago=120)
    # Settled AFTER we submitted, by evidence newer than ours.
    add_ai_result(url, "rejected", "ATS returns gone", "closed")

    rows = {url: {"url": url, "company": "C", "title": "T"}}
    recorded = tasks_verify._record_reverify_results(
        {url: _reverify_result(url, "batch_parked", is_closed=False)}, rows, "m"
    )

    assert recorded == 0
    latest = db.query_one(
        "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert latest["status"] == "rejected", "the fresh closure must still win"
    assert latest["reason"] == "ATS returns gone"


def test_reverify_records_normally_when_nothing_settled_after_submission():
    url = "https://stale.test/2"
    add_ai_result(url, "rejected", "stale closure", "closed")
    # Submitted after that verdict, so our evidence is the newer of the two.
    _submitted_batch("batch_fresh", minutes_ago=0)

    rows = {url: {"url": url, "company": "C", "title": "T"}}
    recorded = tasks_verify._record_reverify_results(
        {url: _reverify_result(url, "batch_fresh", is_closed=False)}, rows, "m"
    )

    assert recorded == 1
    latest = db.query_one(
        "SELECT status FROM ai_queries WHERE url = %s AND check_type = 'closed' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert latest["status"] == "passed", "a reverify with newer evidence still reopens"


def test_reverify_records_when_the_evidence_cannot_be_dated():
    """No registry row means no submitted_at to compare against. Recording is
    the pre-existing behaviour and a url is not dropped on a suspicion."""
    url = "https://stale.test/3"
    add_ai_result(url, "rejected", "stale closure", "closed")

    rows = {url: {"url": url, "company": "C", "title": "T"}}
    recorded = tasks_verify._record_reverify_results(
        {url: _reverify_result(url, "batch_unregistered", is_closed=False)}, rows, "m"
    )
    assert recorded == 1
