"""Provider batch submission, parking, polling, and verdict collection."""

from __future__ import annotations

import pytest

from api import db, fetching, worker
from core import page_fetches
from core.store import add_ai_result
from tasks import runtime as tasks_runtime
from tasks import verify as tasks_verify
from tests.factories import finished
from tests.worker_queue_support import (
    _claim_for_test,
    _collect_from,
    _run_batched,
    _submit_ids,
)
from tests.worker_queue_support import _clear_claim as _clear_claim

pytestmark = pytest.mark.usefixtures("_clear_claim")


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
                text='{"is_closed": false, "reason": "still accepting"}',
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
            for s in specs
        }

    monkeypatch.setattr(fetching, "fetch_page", fake_fetch_page)
    monkeypatch.setattr("core.batch.submit_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_finished_batches", finished(_collect_from(fake_batch)))

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


def test_batch_event_hook_persists_status_and_unique_batch_ids():

    t1 = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1, "user_id": 1})
    hook = tasks_runtime.batch_event_hook(t1, "filter", "gpt-5-nano")
    hook("batch_abc", "validating", {"requests": 10, "completed": 0, "failed": 0})
    hook("batch_abc", "in_progress", {"requests": 10, "completed": 4, "failed": 0})
    hook("batch_abc", "completed", {"requests": 10, "completed": 9, "failed": 1})
    from api import db

    row = db.query_one("SELECT * FROM ai_batches WHERE provider_batch_id = 'batch_abc'")
    assert row["status"] == "completed" and row["completed"] == 9
    assert row["failed_count"] == 1 and row["completed_at"] is not None
    assert tasks_runtime.pending_batch_ids(t1) == ["batch_abc"]
    hook("batch_def", "validating", {"requests": 5, "completed": 0, "failed": 0})
    assert tasks_runtime.pending_batch_ids(t1) == ["batch_abc", "batch_def"]
    hook("batch_abc", "completed", {"requests": 10, "completed": 9, "failed": 1})
    assert tasks_runtime.pending_batch_ids(t1) == ["batch_abc", "batch_def"]


@pytest.mark.asyncio
async def test_verify_new_records_both_verdicts(monkeypatch):
    from api import db
    from core import batch as core_batch

    db.execute(
        "INSERT INTO jobs (url, source, company, title, available) "
        "VALUES ('https://v.test/1', 's', 'Acme', 'SWE', true)"
    )
    page_fetches.record("https://v.test/1", "passed", "scraped", "J" * 500)

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        assert effort == "low"
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text=(
                    '{"is_closed": false, "closed_reason": "still accepting", '
                    '"requires_clearance_or_restrictions": true, '
                    '"clearance_reason": "US citizenship required"}'
                ),
                usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            )
            for s in specs
        }

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    monkeypatch.setattr("core.batch.submit_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_finished_batches", finished(_collect_from(fake_batch)))
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
async def test_batched_verdicts_record_their_reason(monkeypatch):
    """The batch path recorded reason="" for a while, which cost nothing
    measurable in tokens and cost every answer to "why is my board empty":
    100% of new verdicts across all three check types carried no reason at
    all. Each axis must persist its OWN reason - one shared sentence could not
    say which of the two it explained."""
    from api import db
    from core import batch as core_batch

    db.execute(
        "INSERT INTO jobs (url, source, company, title, available) "
        "VALUES ('https://r.test/1', 's', 'Acme', 'SWE', true)"
    )
    page_fetches.record("https://r.test/1", "passed", "scraped", "J" * 500)

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text=(
                    '{"is_closed": true, "closed_reason": "position filled", '
                    '"requires_clearance_or_restrictions": true, '
                    '"clearance_reason": "TS/SCI required"}'
                ),
                usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            )
            for s in specs
        }

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("core.batch.submit_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_finished_batches", finished(_collect_from(fake_batch)))
    tid = tasks_runtime.enqueue("verify_new", {"cycle": "t"})
    await _run_batched(tid, lambda: tasks_verify.handle_verify_new(tid, {"cycle": "t"}))

    reasons = {
        r["check_type"]: r["reason"]
        for r in db.query(
            "SELECT check_type, reason FROM ai_queries WHERE url = 'https://r.test/1' "
            "AND check_type IN ('closed','clearance')"
        )
    }
    assert reasons["closed"] == "position filled"
    assert reasons["clearance"] == "TS/SCI required"


@pytest.mark.asyncio
async def test_reverify_records_batch_verdicts_and_reattaches(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from core import batch as core_batch

    async def fake_collect(batch_ids, on_event=None):
        return [
            core_batch.BatchResult(
                "https://rv.example.com/1",
                text=(
                    '{"is_closed": true, "closed_reason": "position filled", '
                    '"requires_clearance_or_restrictions": false, "clearance_reason": ""}'
                ),
                usage={"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
                batch_id=batch_ids[0],
            )
        ]

    async def fake_fetch_page(url):
        return "job content here", False

    monkeypatch.setattr(fetching, "fetch_page", fake_fetch_page)
    monkeypatch.setattr("core.batch.submit_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_finished_batches", finished(fake_collect))

    rows = [{"url": "https://rv.example.com/1", "company": "Acme", "title": "SWE"}]
    task_id = tasks_runtime.enqueue("reverify_chunk", {"parent_id": 1})
    worker._claim_task()
    await _run_batched(task_id, lambda: tasks_verify._reverify_jobs(task_id, rows))

    row = db.query_one(
        "SELECT status, config_name FROM ai_queries WHERE url = 'https://rv.example.com/1' "
        "AND check_type = 'closed' ORDER BY id DESC LIMIT 1"
    )
    assert row["status"] == "rejected" and row["config_name"] == "reverify"
    # One fetch, one call, both axes: the clearance verdict is refreshed by
    # the same answer rather than being written once and never revisited.
    clearance = db.query_one(
        "SELECT status, config_name FROM ai_queries WHERE url = 'https://rv.example.com/1' "
        "AND check_type = 'clearance' ORDER BY id DESC LIMIT 1"
    )
    assert clearance and clearance["status"] == "passed"
    assert clearance["config_name"] == "reverify"
    # Collected, so nothing is left to reattach to: a rerun cannot download
    # and re-record the same batch. Reattaching to a batch still in flight is
    # covered in test_batch_stragglers.
    assert tasks_runtime.pending_batch_ids(task_id) == []


@pytest.mark.asyncio
async def test_full_sweep_rechecks_even_fresh_verdicts(monkeypatch):
    """A forced sweep must overturn verdicts made today. Skipping them is
    exactly what would preserve the stale-evidence verdicts it exists to fix."""
    from core import batch as core_batch
    from core.fetching import ats as core_ats
    from core.store import add_ai_result

    checked = []

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        checked.extend(s.custom_id for s in specs)
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text='{"is_closed": false, "reason": "still accepting"}',
                usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
            )
            for s in specs
        }

    async def fake_fetch(url):
        return "fresh page text " * 40, False

    monkeypatch.setattr(fetching, "fetch_page", fake_fetch)
    monkeypatch.setattr(core_ats, "resolve", lambda url: core_ats.UNSUPPORTED)
    monkeypatch.setattr("core.batch.submit_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_finished_batches", finished(_collect_from(fake_batch)))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    url = "https://fs.test/1"
    add_ai_result(url, "passed", "job open", "closed")  # verdict from today
    rows = [{"url": url, "company": "C", "title": "T"}]

    tid = tasks_runtime.enqueue("reverify_chunk", {"parent_id": 1})
    await _run_batched(tid, lambda: tasks_verify._reverify_jobs(tid, rows))
    assert checked == [], "normal sweep should skip a verdict made today"

    await _run_batched(tid, lambda: tasks_verify._reverify_jobs(tid, rows, force=True))
    assert checked == [url], "forced sweep must re-check it anyway"


@pytest.mark.asyncio
async def test_polling_records_the_status_it_already_fetched(monkeypatch, f):
    """The poll asks the provider for every batch's status each minute to
    decide whether to resume. It used that answer for one boolean and threw it
    away, so ai_batches.status kept whatever the submitting task last wrote -
    a batch sat at 'validating' for two hours while the provider had it
    'in_progress' at 446 of 501 requests. The column lied in both directions:
    live work read as stuck, and a genuinely stalled batch was indistinguishable
    from a healthy one."""
    from tasks import batches as tasks_batches

    task_id = f.make_task("classify_mail", {"batch_ids": ["batch_live"]}, status="awaiting_batch")
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model, status) "
        "VALUES (%s, %s, 'mail', 'gpt-5.6-luna', 'validating')",
        ("batch_live", task_id),
    )

    async def fake_progress(ids):
        from core.batch import BatchProgress

        return {"batch_live": BatchProgress("in_progress", total=501, completed=446)}

    monkeypatch.setattr("core.batch.batch_progress", fake_progress)
    poll_id = f.make_task("poll_batches", {})
    _claim_for_test(poll_id)
    await tasks_batches.handle_poll_batches(poll_id, {})

    row = db.query_one(
        "SELECT status, completed_at, completed, requests FROM ai_batches "
        "WHERE provider_batch_id = 'batch_live'"
    )
    assert row is not None
    assert row["status"] == "in_progress", "the poll must record what it just read"
    assert row["completed_at"] is None, "in flight is not finished"
    assert (row["completed"], row["requests"]) == (446, 501), (
        "the counts come back on the same call; discarding them is why 'is it "
        "moving or stuck' could only be answered by curling the provider"
    )

    assert (
        db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))["status"]
        == "awaiting_batch"
    ), "a non-terminal batch must not resume the task"


@pytest.mark.asyncio
async def test_polling_stamps_completed_at_once_terminal(monkeypatch, f):
    from tasks import batches as tasks_batches

    task_id = f.make_task("classify_mail", {"batch_ids": ["batch_done"]}, status="awaiting_batch")
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model, status) "
        "VALUES (%s, %s, 'mail', 'gpt-5.6-luna', 'in_progress')",
        ("batch_done", task_id),
    )

    async def fake_progress(ids):
        from core.batch import BatchProgress

        return {"batch_done": BatchProgress("completed", total=10, completed=10)}

    monkeypatch.setattr("core.batch.batch_progress", fake_progress)
    poll_id = f.make_task("poll_batches", {})
    _claim_for_test(poll_id)
    await tasks_batches.handle_poll_batches(poll_id, {})

    row = db.query_one(
        "SELECT status, completed_at FROM ai_batches WHERE provider_batch_id = 'batch_done'"
    )
    assert row is not None
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
