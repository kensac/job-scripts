import json

from api import db
from api.ai import migrate_receipt_payloads
from core.payload_objects import PayloadStore
from tests.test_receipt_verification_bulk import prepare


def test_grouped_compaction_uses_one_conditional_update(f, monkeypatch, capsys):
    from core.pool import pool

    store, rows = prepare(f, 3)
    reads = []
    writes = []
    for name in ("query", "query_one", "execute_count"):
        original = getattr(db, name)

        def measured(sql, params=None, _original=original, _name=name):
            (writes if _name == "execute_count" else reads).append(sql)
            return _original(sql, params)

        monkeypatch.setattr(db, name, measured)
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    monkeypatch.setattr(pool, "close", lambda: None)
    monkeypatch.setenv("PGOPTIONS", "")
    monkeypatch.setattr(
        "sys.argv",
        [
            "migration",
            "compact",
            "--limit",
            "3",
            "--backup-complete",
            "--compact-group-size",
            "3",
            "--compact-workers",
            "2",
            "--compact-byte-budget",
            "100000",
        ],
    )
    assert migrate_receipt_payloads.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"compacted": 3}
    assert result["after"] == [rows[-1]["provider_batch_id"], rows[-1]["custom_id"]]
    assert len(reads) == 4 and len(writes) == 1
    current = db.query("SELECT * FROM batch_result_receipts ORDER BY provider_batch_id,custom_id")
    for original, actual in zip(rows, current, strict=True):
        expected = {**original, "response": dict(original["response"])}
        del expected["response"]["embedding_vectors"]
        assert actual == expected


def compact(
    store, *, after=None, limit=4, group_size=4, workers=2, byte_budget=100000, backup_complete=True
):
    from api.ai.receipt_compaction import compact as run

    return run(
        store,
        after=after,
        limit=limit,
        group_size=group_size,
        workers=workers,
        byte_budget=byte_budget,
        backup_complete=backup_complete,
    )


def test_service_backup_gate_precedes_database_or_object_operations(f, monkeypatch):
    import pytest

    store, _ = prepare(f, 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("missing backup confirmation must prevent all IO")

    monkeypatch.setattr(db, "query", forbidden)
    monkeypatch.setattr(store, "get", forbidden)
    with pytest.raises(ValueError, match="independent database backup"):
        compact(store, backup_complete=False)


def test_compaction_preserves_native_json_and_task_accounting_and_replays_safely(f):
    from tests.test_receipt_payloads import row

    store, rows = prepare(f, 4)
    db.execute(
        'UPDATE batch_result_receipts SET response=response || \'{"precise":0.123456789012345678901234567890,"unicode":"é","ordered":[2,1]}\'::jsonb'
    )
    expected = db.query(
        "SELECT provider_batch_id,custom_id,(response-'embedding_vectors')::text AS response,to_jsonb(r)-'response' AS identity FROM batch_result_receipts r ORDER BY provider_batch_id,custom_id"
    )
    tasks_before = db.query("SELECT * FROM tasks ORDER BY id")
    result = compact(store)
    assert result.counts == {"compacted": 4}
    assert result.logical_bytes_verified == sum(
        item["response"]["embedding_vectors_ref"]["size"] for item in rows
    )
    assert (
        db.query(
            "SELECT provider_batch_id,custom_id,response::text AS response,to_jsonb(r)-'response' AS identity FROM batch_result_receipts r ORDER BY provider_batch_id,custom_id"
        )
        == expected
    )
    assert db.query("SELECT * FROM tasks ORDER BY id") == tasks_before
    assert compact(store).counts == {}
    assert all("embedding_vectors_ref" in row(item["task_id"])["response"] for item in rows)


def test_out_of_order_unavailable_object_commits_only_first_verified_row(f, monkeypatch):
    from threading import Event

    from tests.test_receipt_payloads import row

    store, rows = prepare(f, 4)
    target = rows[1]["response"]["embedding_vectors_ref"]
    del store.client.objects[target["bucket"], target["key"]]
    original = store.get
    second_finished = Event()

    def reorder(ref):
        if ref.key == rows[0]["response"]["embedding_vectors_ref"]["key"]:
            assert second_finished.wait(timeout=10)
        else:
            second_finished.set()
        return original(ref)

    monkeypatch.setattr(store, "get", reorder)
    result = compact(store)
    assert result.counts == {"compacted": 1, "unavailable": 1}
    assert result.after == (rows[0]["provider_batch_id"], rows[0]["custom_id"])
    assert result.logical_bytes_verified == rows[0]["response"]["embedding_vectors_ref"]["size"]
    assert "embedding_vectors" not in row(rows[0]["task_id"])["response"]
    assert [row(item["task_id"]) for item in rows[1:]] == rows[1:]


def test_changed_parent_after_get_stops_before_its_receipt(f, monkeypatch):
    from tests.test_receipt_payloads import row

    store, rows = prepare(f, 4)
    original = store.get

    def reactivate(ref):
        vectors = original(ref)
        if ref.key == rows[1]["response"]["embedding_vectors_ref"]["key"]:
            db.execute("UPDATE tasks SET status='running' WHERE id=%s", (rows[1]["task_id"],))
        return vectors

    monkeypatch.setattr(store, "get", reactivate)
    result = compact(store)
    assert result.counts == {"compacted": 1, "changed": 1}
    assert result.after == (rows[0]["provider_batch_id"], rows[0]["custom_id"])
    assert [row(item["task_id"]) for item in rows[1:]] == rows[1:]


def test_failed_update_rolls_back_current_group_and_preserves_previous_cursor(f, monkeypatch):
    from psycopg import OperationalError

    from tests.test_receipt_payloads import row

    store, rows = prepare(f, 4)
    original = db.execute_count
    updates = 0

    def interrupted(sql, params=None):
        nonlocal updates
        changed = original(sql, params)
        updates += 1
        if updates == 2:
            raise OperationalError("connection interrupted after update")
        return changed

    monkeypatch.setattr(db, "execute_count", interrupted)
    result = compact(store, group_size=2, workers=2)
    assert result.counts == {"compacted": 2, "unavailable": 1}
    assert result.stop_reason == "database_error"
    assert result.after == (rows[1]["provider_batch_id"], rows[1]["custom_id"])
    assert all("embedding_vectors" not in row(item["task_id"])["response"] for item in rows[:2])
    assert [row(item["task_id"]) for item in rows[2:]] == rows[2:]


def test_group_size_and_byte_budget_prevent_unbounded_preparation(f, monkeypatch):
    from tests.test_receipt_payloads import row

    store, rows = prepare(f, 4)

    def forbidden(ref):
        raise AssertionError("oversized group cannot hydrate an object")

    monkeypatch.setattr(store, "get", forbidden)
    result = compact(store, byte_budget=1)
    assert result.counts == {"unavailable": 1} and result.stop_reason == "byte_budget"
    assert result.after is None
    assert [row(item["task_id"]) for item in rows] == rows


def test_parent_lock_precedes_receipt_lock(f, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from core.pool import connection

    store, rows = prepare(f, 1)
    parent_attempted = Event()
    original = db.query

    def observed(sql, params=None):
        if sql.startswith("SELECT id FROM tasks") and "FOR UPDATE" in sql:
            parent_attempted.set()
        return original(sql, params)

    monkeypatch.setattr(db, "query", observed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with connection() as conn, conn.transaction():
            conn.execute("SELECT id FROM tasks WHERE id=%s FOR UPDATE", (rows[0]["task_id"],))
            future = executor.submit(compact, store, limit=1, group_size=1, workers=1)
            assert parent_attempted.wait(timeout=10)
            conn.execute(
                "SELECT provider_batch_id FROM batch_result_receipts WHERE task_id=%s FOR UPDATE NOWAIT",
                (rows[0]["task_id"],),
            )
        result = future.result(timeout=10)
    assert result.counts == {"compacted": 1}


def test_concurrent_gets_finish_before_any_mutation_transaction(f, monkeypatch):
    from contextlib import contextmanager
    from threading import Barrier, Lock

    store, _ = prepare(f, 4)
    original_get, original_transaction = store.get, db.transaction
    barrier = Barrier(2)
    lock = Lock()
    transactions = active = peak = 0

    @contextmanager
    def tracked_transaction():
        nonlocal transactions
        with original_transaction():
            with lock:
                transactions += 1
            try:
                yield
            finally:
                with lock:
                    transactions -= 1

    def concurrent(ref):
        nonlocal active, peak
        with lock:
            assert transactions == 0
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=10)
            return original_get(ref)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(db, "transaction", tracked_transaction)
    monkeypatch.setattr(store, "get", concurrent)
    assert compact(store).counts == {"compacted": 4}
    assert peak == 2 and active == transactions == 0


def test_conditional_write_mismatch_rolls_back_the_entire_group(f, monkeypatch):
    from tests.test_receipt_payloads import row

    store, rows = prepare(f, 4)
    original = db.execute_count

    def mismatched(sql, params=None):
        return original(sql, params) - 1

    monkeypatch.setattr(db, "execute_count", mismatched)
    result = compact(store)
    assert result.counts == {"changed": 1}
    assert result.after is None and result.logical_bytes_verified == 0
    assert [row(item["task_id"]) for item in rows] == rows


def test_compaction_serial_and_grouped_workload(f, monkeypatch, request):
    import time
    from threading import Event, Lock

    from api.ai import receipt_payloads

    store, rows = prepare(f, 6)
    original_get = store.get
    lock = Lock()
    active = peak = gets = selects = updates = 0
    # Controlled synthetic latency, not a model of the deployed object store.
    object_latency_seconds = 0.01

    def delayed(ref):
        nonlocal active, peak, gets
        with lock:
            active += 1
            peak = max(peak, active)
            gets += 1
        try:
            Event().wait(object_latency_seconds)
            return original_get(ref)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(store, "get", delayed)
    for name in ("query", "query_one", "execute_count"):
        original = getattr(db, name)

        def measured(sql, params=None, _original=original, _name=name):
            nonlocal selects, updates
            if _name == "execute_count":
                updates += 1
            else:
                selects += 1
            return _original(sql, params)

        monkeypatch.setattr(db, name, measured)
    started = time.perf_counter()
    cursor = None
    for _ in rows:
        source = receipt_payloads.candidates(after=cursor, limit=1, mode="compact")[0]
        assert receipt_payloads.migrate(source, store, mode="compact") == "compacted"
        cursor = (source["provider_batch_id"], source["custom_id"])
    baseline = {
        "selects": selects,
        "updates": updates,
        "gets": gets,
        "peak_gets": peak,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }
    assert baseline["selects"] == 30 and baseline["updates"] == baseline["gets"] == 6
    expected = db.query(
        "SELECT to_jsonb(r)::text AS exact FROM batch_result_receipts r ORDER BY provider_batch_id,custom_id"
    )
    db.executemany(
        "UPDATE batch_result_receipts SET response=%s WHERE task_id=%s",
        [(db.jsonb(item["response"]), item["task_id"]) for item in rows],
    )
    selects = updates = gets = peak = 0
    started = time.perf_counter()
    result = compact(store, limit=6, group_size=3, workers=3)
    improved = {
        "selects": selects,
        "updates": updates,
        "gets": gets,
        "peak_gets": peak,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }
    assert result.counts == {"compacted": 6} and result.after == cursor
    assert improved["selects"] == 8 and improved["updates"] == 2 and improved["gets"] == 6
    assert improved["peak_gets"] <= 3
    assert (
        db.query(
            "SELECT to_jsonb(r)::text AS exact FROM batch_result_receipts r ORDER BY provider_batch_id,custom_id"
        )
        == expected
    )
    request.node.user_properties.append(
        (
            "receipt_compaction_workload",
            json.dumps(
                {
                    "rows": 6,
                    "fake_get_latency_ms": object_latency_seconds * 1000,
                    "before": baseline,
                    "after": improved,
                }
            ),
        )
    )
