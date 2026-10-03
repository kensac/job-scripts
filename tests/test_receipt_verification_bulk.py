import json

import pytest

from api import db
from api.ai import migrate_receipt_payloads, receipt_payloads
from core.payload_objects import PayloadStore
from tests.test_receipt_payloads import ObjectClient, receipt, row


def prepare(f, count):
    store = PayloadStore(ObjectClient(), "test-payloads")
    tasks = []
    for _ in range(count):
        task, _ = receipt(f)
        db.execute(
            "UPDATE batch_result_receipts SET response=jsonb_set(response,'{embedding_vectors}',%s) WHERE task_id=%s",
            (db.jsonb([[float(task), 0.25]]), task),
        )
        receipt_payloads.migrate(row(task), store, mode="copy")
        tasks.append(task)
    return store, sorted(
        (row(task) for task in tasks),
        key=lambda item: (item["provider_batch_id"], item["custom_id"]),
    )


def cli(monkeypatch, store, *options):
    from core.pool import pool

    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    monkeypatch.setattr(pool, "close", lambda: None)
    monkeypatch.setenv("PGOPTIONS", "")
    monkeypatch.setattr("sys.argv", ["migration", "verify", "--limit", "3", *options])
    return migrate_receipt_payloads.main()


def test_serial_verification_stops_before_changed_row(f, monkeypatch, capsys):
    store, rows = prepare(f, 3)
    original = store.get
    calls = 0

    def change_during_get(ref):
        nonlocal calls
        calls += 1
        result = original(ref)
        if calls == 2:
            db.execute("UPDATE tasks SET status='running' WHERE id=%s", (rows[1]["task_id"],))
        return result

    monkeypatch.setattr(store, "get", change_during_get)
    assert cli(monkeypatch, store) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["after"] == [rows[0]["provider_batch_id"], rows[0]["custom_id"]]
    assert result["counts"] == {"verified": 1, "changed": 1}
    assert calls == 2


def test_grouped_verification_bounds_database_queries(f, monkeypatch, capsys):
    store, rows = prepare(f, 3)
    calls = []
    for name in ("query", "query_one"):
        original = getattr(db, name)

        def measured(sql, params=None, _original=original):
            calls.append(sql)
            return _original(sql, params)

        monkeypatch.setattr(db, name, measured)
    assert (
        cli(
            monkeypatch,
            store,
            "--verify-group-size",
            "3",
            "--verify-workers",
            "2",
            "--verify-byte-budget",
            "100000",
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"verified": 3}
    assert result["after"] == [rows[-1]["provider_batch_id"], rows[-1]["custom_id"]]
    assert len(calls) == 3


def bounds(rows):
    return db.query_one(
        "SELECT max(octet_length(to_jsonb(r)::text)+(r.response->'embedding_vectors_ref'->>'size')::bigint) AS largest "
        "FROM batch_result_receipts r WHERE task_id=ANY(%s)",
        ([item["task_id"] for item in rows],),
    )["largest"]


def grouped(store, **kwargs):
    from api.ai.receipt_verification import verify

    return verify(store, after=None, limit=4, group_size=4, workers=2, byte_budget=100000, **kwargs)


def test_object_concurrency_is_real_bounded_and_has_no_open_db_transaction(f, monkeypatch):
    from threading import Barrier, Lock

    from core.pool import in_transaction

    store, _ = prepare(f, 4)
    original = store.get
    barrier = Barrier(2)
    lock = Lock()
    active = peak = calls = 0

    def concurrent(ref):
        nonlocal active, peak, calls
        assert not in_transaction()
        with lock:
            active += 1
            peak = max(peak, active)
            calls += 1
        try:
            barrier.wait(timeout=10)
            return original(ref)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(store, "get", concurrent)
    result = grouped(store)
    assert result.counts == {"verified": 4}
    assert peak == 2 and calls == 4 and active == 0


def test_byte_budget_limits_hydrated_group_and_oversized_row_stops_before_get(f, monkeypatch):
    from api.ai.receipt_verification import verify

    store, rows = prepare(f, 3)
    budget = bounds(rows)
    calls = []
    original = db.query

    def counted(sql, params=None):
        calls.append(sql)
        return original(sql, params)

    monkeypatch.setattr(db, "query", counted)
    result = verify(store, after=None, limit=3, group_size=3, workers=2, byte_budget=budget)
    assert result.counts == {"verified": 3}
    assert len(calls) == 9
    calls.clear()

    def forbidden(ref):
        raise AssertionError("oversized payload must not be loaded")

    monkeypatch.setattr(store, "get", forbidden)
    result = verify(store, after=None, limit=3, group_size=3, workers=2, byte_budget=1)
    assert result.counts == {"unavailable": 1}
    assert result.stop_reason == "byte_budget" and result.after is None
    assert len(calls) == 1 and " AS snapshot " not in calls[0]


def test_out_of_order_object_failure_keeps_only_verified_cursor_prefix(f, monkeypatch):
    from threading import Event

    store, rows = prepare(f, 4)
    original = store.get
    first_finished = Event()
    references = [item["response"]["embedding_vectors_ref"] for item in rows]
    from core.payload_objects import PayloadRef

    second = PayloadRef.parse(references[1])
    store.client.objects[second.bucket, second.key] = b"corrupt"

    def reordered(ref):
        if ref.key == references[0]["key"]:
            assert first_finished.wait(timeout=10)
        else:
            first_finished.set()
        return original(ref)

    monkeypatch.setattr(store, "get", reordered)
    result = grouped(store)
    assert result.counts == {"verified": 1, "unavailable": 1}
    assert result.after == (rows[0]["provider_batch_id"], rows[0]["custom_id"])
    assert result.logical_bytes_verified == references[0]["size"]


@pytest.mark.parametrize(
    "change", ["status", "kind", "consumed", "outcome", "model", "response", "deleted"]
)
def test_group_recheck_detects_eligibility_and_exact_row_changes(f, monkeypatch, change):

    store, rows = prepare(f, 4)
    original = store.get
    target = rows[1]

    def mutate(ref):
        value = original(ref)
        if ref.key == target["response"]["embedding_vectors_ref"]["key"]:
            statements = {
                "status": "UPDATE tasks SET status='running' WHERE id=%s",
                "kind": "UPDATE tasks SET kind='classify_job_profiles' WHERE id=%s",
                "consumed": "UPDATE batch_result_receipts SET consumed_at=NULL WHERE task_id=%s",
                "outcome": "UPDATE batch_result_receipts SET outcome=NULL WHERE task_id=%s",
                "model": "UPDATE batch_result_receipts SET model='changed-model' WHERE task_id=%s",
                "response": "UPDATE batch_result_receipts SET response=response || jsonb_build_object('error',repeat('x',1000000)) WHERE task_id=%s",
                "deleted": "DELETE FROM batch_result_receipts WHERE task_id=%s",
            }
            db.execute(statements[change], (target["task_id"],))
        return value

    monkeypatch.setattr(store, "get", mutate)
    result = grouped(store)
    assert result.counts == {"verified": 1, "changed": 1}
    assert result.after == (rows[0]["provider_batch_id"], rows[0]["custom_id"])
    with db.transaction(), pytest.raises(RuntimeError, match="inside a database transaction"):
        grouped(store)


def test_grouped_verify_is_read_only_and_keeps_receipts_unchanged(f, monkeypatch):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    import core.pool as connections

    store, rows = prepare(f, 4)
    before = [row(item["task_id"]) for item in rows]
    with (
        ConnectionPool(
            connections.DATABASE_URL,
            min_size=1,
            max_size=1,
            kwargs={"row_factory": dict_row, "options": "-c default_transaction_read_only=on"},
        ) as readonly,
        monkeypatch.context() as patch,
    ):
        patch.setattr(connections, "pool", readonly)
        result = grouped(store)
        assert result.counts == {"verified": 4}
    assert [row(item["task_id"]) for item in rows] == before


def test_serial_and_grouped_workload_measurements(f, monkeypatch, request):
    import time
    from threading import Event, Lock

    from api.ai.receipt_verification import verify

    store, rows = prepare(f, 6)
    original_get = store.get
    lock = Lock()
    active = peak = gets = queries = 0
    # Controlled synthetic IO latency, not a production latency model.
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
    for name in ("query", "query_one"):
        original = getattr(db, name)

        def measured(sql, params=None, _original=original):
            nonlocal queries
            queries += 1
            return _original(sql, params)

        monkeypatch.setattr(db, name, measured)
    before = [row(item["task_id"]) for item in rows]
    queries = 0
    started = time.perf_counter()
    cursor = None
    for _ in rows:
        source = receipt_payloads.candidates(after=cursor, limit=1, mode="verify")[0]
        assert receipt_payloads.migrate(source, store, mode="verify") == "verified"
        cursor = (source["provider_batch_id"], source["custom_id"])
    baseline = {
        "selects": queries,
        "gets": gets,
        "peak_gets": peak,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }
    assert baseline["selects"] == 30 and baseline["peak_gets"] == 1
    queries = gets = peak = 0
    started = time.perf_counter()
    result = verify(store, after=None, limit=6, group_size=3, workers=3, byte_budget=100000)
    improved = {
        "selects": queries,
        "gets": gets,
        "peak_gets": peak,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }
    assert result.counts == {"verified": 6} and result.after == cursor
    assert improved["selects"] == 6 and improved["gets"] == 6 and improved["peak_gets"] <= 3
    assert [row(item["task_id"]) for item in rows] == before
    request.node.user_properties.append(
        (
            "receipt_verification_workload",
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


@pytest.mark.parametrize("failure", ["inline_mismatch", "missing", "invalid_reference_size"])
def test_integrity_failure_after_verified_prefix_is_not_counted_or_skipped(f, failure):
    store, rows = prepare(f, 4)
    second = rows[1]
    ref = second["response"]["embedding_vectors_ref"]
    if failure == "inline_mismatch":
        db.execute(
            "UPDATE batch_result_receipts SET response=jsonb_set(response,'{embedding_vectors}','[[999]]') WHERE task_id=%s",
            (second["task_id"],),
        )
    elif failure == "missing":
        del store.client.objects[ref["bucket"], ref["key"]]
    else:
        db.execute(
            "UPDATE batch_result_receipts SET response=jsonb_set(response,'{embedding_vectors_ref,size}','true') WHERE task_id=%s",
            (second["task_id"],),
        )
    result = grouped(store)
    assert result.counts == {"verified": 1, "unavailable": 1}
    assert result.after == (rows[0]["provider_batch_id"], rows[0]["custom_id"])
    assert result.logical_bytes_verified == rows[0]["response"]["embedding_vectors_ref"]["size"]


def test_compacted_references_verify_without_inline_arrays(f):
    store, rows = prepare(f, 4)
    db.execute("UPDATE batch_result_receipts SET response=response-'embedding_vectors'")
    before = [row(item["task_id"]) for item in rows]
    result = grouped(store)
    assert result.counts == {"verified": 4}
    assert [row(item["task_id"]) for item in rows] == before


@pytest.mark.parametrize("operation", ["verify", "compact"])
def test_grouped_scan_budget_pages_past_missing_references(f, operation):
    from api.ai.receipt_compaction import compact
    from api.ai.receipt_verification import verify

    store, rows = prepare(f, 5)
    for item in rows[:3]:
        db.execute(
            "UPDATE batch_result_receipts SET response=response-'embedding_vectors_ref' WHERE task_id=%s",
            (item["task_id"],),
        )
    run = verify if operation == "verify" else compact
    options = {} if operation == "verify" else {"backup_complete": True}
    first = run(
        store,
        after=None,
        limit=2,
        group_size=2,
        workers=1,
        byte_budget=100000,
        scan_limit=3,
        **options,
    )
    assert first.counts == {"skipped": 3}
    assert first.scanned == 3 and first.verified_after is None
    assert first.after == (rows[2]["provider_batch_id"], rows[2]["custom_id"])
    second = run(
        store,
        after=first.after,
        limit=2,
        group_size=2,
        workers=1,
        byte_budget=100000,
        scan_limit=3,
        **options,
    )
    assert second.counts == {"verified" if operation == "verify" else "compacted": 2}
    assert (
        second.after
        == second.verified_after
        == (rows[4]["provider_batch_id"], rows[4]["custom_id"])
    )


@pytest.mark.parametrize("operation", ["verify", "compact"])
def test_skipped_keys_never_advance_past_bad_reference(f, operation):
    from api.ai.receipt_compaction import compact
    from api.ai.receipt_verification import verify

    store, rows = prepare(f, 3)
    db.execute(
        "UPDATE batch_result_receipts SET response=response-'embedding_vectors_ref' WHERE task_id=%s",
        (rows[0]["task_id"],),
    )
    db.execute(
        "UPDATE batch_result_receipts SET response=jsonb_set(response,'{embedding_vectors_ref,size}','null') WHERE task_id=%s",
        (rows[1]["task_id"],),
    )
    run = verify if operation == "verify" else compact
    result = run(
        store,
        after=None,
        limit=3,
        group_size=3,
        workers=1,
        byte_budget=100000,
        scan_limit=3,
        **({} if operation == "verify" else {"backup_complete": True}),
    )
    assert result.counts == {"skipped": 1, "unavailable": 1}
    assert result.after == (rows[0]["provider_batch_id"], rows[0]["custom_id"])
    assert result.verified_after is None and result.stop_reason == "invalid_reference_size"
