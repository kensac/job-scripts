import pytest

from api import db
from core.batch import BatchResult
from tasks import filters, runtime


@pytest.mark.asyncio
async def test_collected_result_survives_crash_before_consumer(f, monkeypatch):
    task_id = f.make_task("run_filter_batch_chunk", {"batch_ids": ["paid"]}, status="running")

    async def collect(ids, hook):
        return [BatchResult("url", text="paid response", batch_id="paid")], []

    monkeypatch.setattr("core.batch.collect_finished_batches", collect)
    first = await runtime.collect_pending(task_id, None)
    assert first
    # The process exits here, before the handler writes any result.
    recovered = await runtime.collect_pending(task_id, None)
    assert recovered


@pytest.mark.asyncio
async def test_replaying_consumed_filter_result_does_not_duplicate_user_usage(f, monkeypatch):
    uid = f.make_user()
    job_id = f.make_job()
    job = db.query_one("SELECT id,url,company,title FROM jobs WHERE id=%s", (job_id,))
    flt = f.make_filter(uid, on_ambiguous="keep")
    payload = {
        "user_id": uid,
        "filter": flt,
        "jobs": [job],
        "parent_id": None,
        "batch_ids": ["paid"],
    }
    task_id = f.make_task("run_filter_batch_chunk", payload, status="running")
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id,task_id,purpose,model) VALUES ('paid',%s,'filter','gpt-5-mini')",
        (task_id,),
    )

    async def collect(ids, hook):
        return [
            BatchResult(
                job["url"],
                text='{"should_filter":false,"reason":"fits"}',
                usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
                batch_id="paid",
            )
        ], []

    monkeypatch.setattr("core.batch.collect_finished_batches", collect)
    await filters.handle_run_filter_batch_chunk(task_id, payload)
    db.execute("UPDATE tasks SET payload=%s WHERE id=%s", (db.jsonb(payload), task_id))
    await filters.handle_run_filter_batch_chunk(task_id, payload)
    assert db.query_one("SELECT count(*) AS n FROM api_usage WHERE user_id=%s", (uid,))["n"] == 1


def test_receipt_transaction_rolls_back_verdict_usage_and_ack_together(f):
    from api import budget
    from api.ai import batch_results
    from core.batch import BatchSpec
    from core.store import add_ai_result

    uid = f.make_user()
    task_id = f.make_task("run_filter_batch_chunk", {"user_id": uid})
    result = f.make_batch_result(
        task_id,
        BatchSpec("url", "rules", "original input", "Verdict", {}),
        text="answer",
        model="gpt-5-mini",
    )
    with (
        pytest.raises(RuntimeError, match="crash"),
        batch_results.consume_result(task_id, result) as receipt,
    ):
        assert receipt.pending
        add_ai_result("url", "passed", "fits", "custom", model="gpt-5-mini")
        budget.record_usage(uid, "owner", "filter", "gpt-5-mini", 100, 10, 110, batched=True)
        raise RuntimeError("crash")
    assert db.query_one("SELECT count(*) AS n FROM ai_queries")["n"] == 0
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 0
    assert len(batch_results.unconsumed(task_id)) == 1
    with batch_results.consume_result(task_id, result) as receipt:
        assert receipt.pending
        budget.record_usage(uid, "owner", "filter", "gpt-5-mini", 100, 10, 110, batched=True)
        receipt.outcome = "written"
    with batch_results.consume_result(task_id, result) as replay:
        assert replay.pending is False
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 1


def test_fleet_usage_and_batch_totals_rollback_together(f, monkeypatch):
    from api import budget

    task_id = f.make_task("extract_comp", {})
    hook = runtime.batch_event_hook(task_id, "comp", "gpt-5-mini")
    hook("fleet", "submitted", {"requests": 1})
    original = budget.record_fleet_usage

    def crash(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(budget, "record_fleet_usage", crash)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        hook("fleet", "completed", {"input_tokens": 100, "output_tokens": 10})
    assert (
        db.query_one("SELECT input_tokens FROM ai_batches WHERE provider_batch_id='fleet'")[
            "input_tokens"
        ]
        == 0
    )
    monkeypatch.setattr(budget, "record_fleet_usage", original)
    hook("fleet", "completed", {"input_tokens": 100, "output_tokens": 10})
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 1


def test_checkpoint_refuses_receipt_owned_by_another_task(f):
    from api.ai import batch_results

    first = f.make_task("extract_comp", {})
    second = f.make_task("extract_comp", {"batch_ids": ["same"]})
    result = BatchResult("url", batch_id="same")
    batch_results.checkpoint(first, [result], [])
    with pytest.raises(ValueError, match="another task"):
        batch_results.checkpoint(second, [result], [])
    assert runtime.pending_batch_ids(second) == ["same"]


def test_request_snapshot_retries_and_collected_input_use_original_bytes(f):
    from api.ai.batch_results import checkpoint, snapshot_specs, unconsumed
    from core.batch import BatchSpec

    tid = f.make_task("run_filter_batch_chunk", {}, status="running")
    original = BatchSpec(
        "url", "original prompt", "original page", "Verdict", {}, context={"version": 1}
    )
    changed = BatchSpec("url", "new prompt", "new page", "Verdict", {}, context={"version": 2})
    assert snapshot_specs(tid, [original]) == [original]
    assert snapshot_specs(tid, [changed]) == [original]
    assert not runtime.has_batch_work(tid)
    checkpoint(tid, [BatchResult("url", batch_id="paid")], [])
    assert unconsumed(tid)[0].request == original


@pytest.mark.asyncio
async def test_empty_terminal_collection_survives_crash_without_resubmitting(f, monkeypatch):
    from tasks.application import APPLICATION_TASK

    tid = f.make_task("application_draft", {"batch_ids": ["failed-batch"]}, status="running")

    async def empty_terminal(ids, hook):
        assert ids == ["failed-batch"]
        return [], []

    def no_new_submission(*args, **kwargs):
        raise AssertionError("an already collected terminal batch must not resolve or resubmit")

    monkeypatch.setattr("core.batch.collect_finished_batches", empty_terminal)
    assert await runtime.collect_pending(tid, None) == []
    assert runtime.pending_batch_ids(tid) == []
    monkeypatch.setattr(runtime, "resolve", no_new_submission)
    assert runtime.has_batch_work(tid)
    results, _ = await runtime.run_batched(tid, APPLICATION_TASK, [])
    assert results == []
    assert await runtime.submit_or_collect(tid, [], "unused", "", 1, None) == []
