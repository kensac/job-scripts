import pytest

from api import db, worker
from core.payload_objects import PayloadUnavailable
from tasks import runtime


@pytest.mark.asyncio
async def test_unavailable_snapshot_preserves_paid_batch_provenance(monkeypatch):
    async def unavailable(task_id, payload):
        raise PayloadUnavailable("Required request snapshot unavailable")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", unavailable)
    task_id = runtime.enqueue(
        "test_kind", {"batch_ids": ["paid-batch"], "batch_collection_checkpointed": True}
    )
    await worker.run_once()
    row = db.query_one("SELECT status,payload FROM tasks WHERE id=%s", (task_id,))
    assert row["status"] == "failed"
    assert row["payload"]["batch_ids"] == ["paid-batch"]
    assert row["payload"]["batch_collection_checkpointed"] is True
    assert row["payload"]["payload_recovery"]["reason"] == "payload_unavailable"


@pytest.mark.asyncio
async def test_payload_outage_does_not_exhaust_attempt_budget(monkeypatch):
    async def unavailable(task_id, payload):
        raise PayloadUnavailable("object missing")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", unavailable)
    task_id = runtime.enqueue("test_kind", {"batch_ids": ["paid"]})
    db.execute(
        "UPDATE tasks SET attempts=%s WHERE id=%s",
        (db.get_config("task_max_attempts") - 1, task_id),
    )
    await worker.run_once()
    assert (
        db.query_one("SELECT attempts FROM tasks WHERE id=%s", (task_id,))["attempts"]
        == db.get_config("task_max_attempts") - 1
    )


def recoverable(f, *, parent=None, kind="verify_new"):
    payload = {"batch_ids": ["paid"], "payload_recovery": {"reason": "payload_unavailable"}}
    if parent is not None:
        payload["parent_id"] = parent
    if kind == "run_filter_batch_chunk":
        # Every chunk is enqueued with its job list, and recovery verifies it.
        payload["jobs"] = [{"url": "https://posting"}]
    task_id = f.make_task(kind, payload, status="failed")
    if parent is not None:
        db.execute("UPDATE tasks SET parent_id=%s WHERE id=%s", (parent, task_id))
    return task_id


def test_operator_retry_keeps_original_paid_work(f):
    from tasks.runtime.payload_recovery import retry

    task_id = recoverable(f)
    assert retry(task_id) == "pending"
    task = db.query_one("SELECT payload,status FROM tasks WHERE id=%s", (task_id,))
    assert task == {"payload": {"batch_ids": ["paid"]}, "status": "pending"}
    assert retry(task_id) == "conflict"


def test_recovery_preflights_receipt_vectors(f, monkeypatch):
    from dataclasses import asdict

    from api.ai import batch_results
    from core.batch import BatchResult
    from core.payload_objects import PayloadStore
    from tasks.runtime.payload_recovery import retry
    from tests.factories import ObjectClient

    objects = PayloadStore(ObjectClient(), "test-payloads")
    task_id = recoverable(f)
    result = BatchResult("request", batch_id="paid")
    batch_results.checkpoint(task_id, [result], [])
    ref = objects.put_verified([[0.1]])
    db.execute(
        "UPDATE batch_result_receipts SET response=response || %s WHERE task_id=%s",
        (db.jsonb({"embedding_vectors_ref": asdict(ref)}), task_id),
    )
    saved = dict(objects.client.objects)
    objects.client.objects.clear()
    assert retry(task_id, objects) == "unavailable"
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (task_id,))["status"] == "failed"
    objects.client.objects.update(saved)
    assert retry(task_id, objects) == "pending"


def test_parent_recovery_returns_to_waiting_without_rerun(f):
    from tasks.runtime.payload_recovery import retry

    parent = f.make_task("run_filter", {}, status="failed")
    db.execute("UPDATE tasks SET error='1 chunk(s) failed' WHERE id=%s", (parent,))
    child = recoverable(f, parent=parent, kind="run_filter_batch_chunk")
    assert retry(child) == "pending"
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (parent,))["status"] == "waiting"
    runtime.maybe_finalize_parent(parent)
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (parent,))["status"] == "waiting"


@pytest.mark.parametrize(
    "status,error", [("cancelled", None), ("done", None), ("failed", "unrelated")]
)
def test_parent_conflicts_never_requeue_child(f, status, error):
    from tasks.runtime.payload_recovery import retry

    parent = f.make_task("run_filter", {}, status=status)
    db.execute("UPDATE tasks SET error=%s WHERE id=%s", (error, parent))
    child = recoverable(f, parent=parent, kind="run_filter_batch_chunk")
    assert retry(child) == "conflict"
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (child,))["status"] == "failed"


def test_parent_finalization_serializes_with_recovery(f, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from tasks.runtime.payload_recovery import retry

    parent = f.make_task("run_filter", {}, status="waiting")
    child = recoverable(f, parent=parent, kind="run_filter_batch_chunk")
    counted = threading.Event()
    release = threading.Event()
    retry_lock = threading.Event()
    retry_pid = []
    original = db.query_one

    def intercept(sql, params=None):
        if (
            threading.current_thread().name.startswith("recovery")
            and "FOR UPDATE" in sql
            and params == (parent,)
        ):
            retry_pid.append(original("SELECT pg_backend_pid() AS pid")["pid"])
            retry_lock.set()
        result = original(sql, params)
        if (
            threading.current_thread().name.startswith("finalizer")
            and "status IN ('pending','running','awaiting_batch')" in sql
        ):
            counted.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(db, "query_one", intercept)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="finalizer") as finalizers,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="recovery") as retries,
    ):
        final = finalizers.submit(runtime.maybe_finalize_parent, parent)
        assert counted.wait(5)
        recovered = retries.submit(retry, child)
        try:
            assert retry_lock.wait(5)
            deadline = time.monotonic() + 3
            blocked = False
            while time.monotonic() < deadline:
                blocked = bool(
                    original(
                        "SELECT cardinality(pg_blocking_pids(%s))>0 AS blocked", (retry_pid[0],)
                    )["blocked"]
                )
                if blocked or recovered.done():
                    break
                time.sleep(0.01)
            assert blocked, "recovery must wait for the parent finalizer's transaction"
        finally:
            release.set()
        final.result(timeout=5)
        assert recovered.result(timeout=5) == "conflict"
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (child,))["status"] == "failed"
    assert retry(child) == "pending"
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (parent,))["status"] == "waiting"


def test_recovery_refuses_concurrent_source_change(f, monkeypatch):
    from api.ai import request_snapshots
    from api.ai.batch_results import snapshot_specs
    from core.batch import BatchSpec
    from tasks.runtime.payload_recovery import retry

    task_id = recoverable(f)
    snapshot_specs(task_id, [BatchSpec("request")])
    original = request_snapshots.resolve

    def changed(row, store=None, cache=None):
        value = original(row, store, cache)
        db.execute(
            "UPDATE tasks SET payload=payload || %s WHERE id=%s",
            (db.jsonb({"changed": True}), task_id),
        )
        return value

    monkeypatch.setattr(request_snapshots, "resolve", changed)
    assert retry(task_id) == "conflict"
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (task_id,))["status"] == "failed"


def test_recovery_refuses_accepted_work_without_replay_state(f):
    from tasks.runtime.payload_recovery import retry

    task_id = recoverable(f)
    db.execute("UPDATE tasks SET payload=payload-'batch_ids' WHERE id=%s", (task_id,))
    db.execute(
        "INSERT INTO ai_batches(provider_batch_id,task_id,purpose,model,status) "
        "VALUES ('paid',%s,'verify','model','completed')",
        (task_id,),
    )
    assert retry(task_id) == "conflict"


@pytest.mark.asyncio
async def test_restored_task_consumes_original_receipt_without_new_submission(f, monkeypatch):
    from api.ai import batch_results
    from core import batch
    from core.payload_objects import PayloadStore
    from tasks.runtime.payload_recovery import retry
    from tests.factories import ObjectClient

    objects = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda: objects)
    task_id = f.make_task("test_kind", {}, status="done")
    batch_results.snapshot_specs(task_id, [batch.BatchSpec("request", input="original")])
    result = batch.BatchResult("request", text="paid answer", batch_id="paid")
    batch_results.checkpoint(task_id, [result], [])
    db.execute("UPDATE tasks SET status='pending' WHERE id=%s", (task_id,))
    saved = dict(objects.client.objects)
    objects.client.objects.clear()
    consumed = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("paid work must never be resubmitted")

    async def handler(tid, payload):
        results = await runtime.submit_or_collect(tid, [], "model", "", 100, None)
        for restored in results:
            with batch_results.consume_result(tid, restored) as receipt:
                if receipt.pending:
                    consumed.append((restored.text, restored.request.input))
                    receipt.outcome = "written"

    monkeypatch.setattr(batch, "submit_responses_batches", forbidden)
    monkeypatch.setitem(worker.HANDLERS, "test_kind", handler)
    await worker.run_once()
    assert consumed == []
    assert retry(task_id, objects) == "unavailable"
    objects.client.objects.update(saved)
    assert retry(task_id, objects) == "pending"
    await worker.run_once()
    assert consumed == [("paid answer", "original")]
    assert batch_results.progress_counts(task_id) == (1, 1)
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (task_id,))["status"] == "done"


def test_completed_collection_recovery_ignores_consumed_only_missing_snapshot(f, monkeypatch):
    from api.ai import batch_results, request_snapshots
    from core.batch import BatchResult, BatchSpec
    from core.payload_objects import PayloadStore
    from tasks.runtime.payload_recovery import retry

    objects = PayloadStore.from_env()
    task_id = f.make_task("verify_new", {}, status="done")
    # One call each, so each request is its own bundle.
    batch_results.snapshot_specs(task_id, [BatchSpec("consumed")])
    batch_results.snapshot_specs(task_id, [BatchSpec("required")])
    sources = db.query(request_snapshots.ROWS, (task_id,))
    results = [
        BatchResult(custom_id, text="answer", batch_id="paid")
        for custom_id in ("consumed", "required")
    ]
    batch_results.checkpoint(task_id, results, [])
    with batch_results.consume_result(task_id, results[0]) as receipt:
        receipt.outcome = "written"
    db.execute(
        "UPDATE tasks SET status='failed',payload=payload || %s WHERE id=%s",
        (db.jsonb({"payload_recovery": {"reason": "payload_unavailable"}}), task_id),
    )
    consumed_ref = sources[0]["snapshot_ref"]
    del objects.client.objects[consumed_ref["bucket"], consumed_ref["key"]]
    saved = dict(objects.client.objects)
    objects.client.objects.clear()
    assert retry(task_id, objects) == "unavailable"
    objects.client.objects.update(saved)
    assert retry(task_id, objects) == "pending"


@pytest.mark.parametrize(
    "change", [{"batch_collection_checkpointed": False}, {"batch_ids": ["later-batch"]}]
)
def test_recovery_rechecks_collection_boundary_after_hydration(f, monkeypatch, change):
    from api.ai import batch_results, request_snapshots
    from core.batch import BatchResult, BatchSpec
    from tasks.runtime.payload_recovery import retry

    task_id = recoverable(f)
    batch_results.snapshot_specs(task_id, [BatchSpec("required")])
    batch_results.checkpoint(task_id, [BatchResult("required", batch_id="paid")], [])
    original = request_snapshots.resolve

    def changed(row, store=None, cache=None):
        spec = original(row, store, cache)
        db.execute(
            "UPDATE tasks SET payload=payload || %s WHERE id=%s", (db.jsonb(change), task_id)
        )
        return spec

    monkeypatch.setattr(request_snapshots, "resolve", changed)
    assert retry(task_id) == "conflict"
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (task_id,))["status"] == "failed"
