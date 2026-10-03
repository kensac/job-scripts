from dataclasses import asdict, replace

import pytest

from api import db
from api.ai import batch_results, request_snapshots, snapshot_payloads
from core.batch import BatchResult, BatchSpec
from core.payload_objects import PayloadStore, PayloadUnavailable
from tests.test_receipt_payloads import ObjectClient


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    return store


def request(f, *, status="done", kind="verify_new"):
    task_id = f.make_task(kind, {}, status=status)
    spec = BatchSpec("request", "original rules", "original page", context={"generation": 1})
    batch_results.snapshot_specs(task_id, [spec])
    return task_id, spec


def row(task_id):
    return db.query_one("SELECT * FROM batch_requests WHERE task_id=%s", (task_id,))


def compact(task_id, objects):
    assert snapshot_payloads.migrate(row(task_id), objects, mode="copy") == "copied"
    assert snapshot_payloads.migrate(row(task_id), objects, mode="compact") == "compacted"


def test_round_trip_preserves_immutable_snapshot_identity_and_replay(f, objects):
    task_id, spec = request(f)
    original = row(task_id)
    compact(task_id, objects)
    assert row(task_id)["snapshot"] is None
    assert request_snapshots.resolve(row(task_id)) == spec
    assert batch_results.snapshot_specs(task_id, [replace(spec, input="new page")]) == [spec]
    assert row(task_id)["snapshot"] is None
    assert snapshot_payloads.migrate(row(task_id), objects, mode="verify") == "verified"
    assert snapshot_payloads.migrate(row(task_id), objects, mode="restore") == "restored"
    assert row(task_id) == original


def test_unconsumed_requires_object_before_consumption(f, objects):
    task_id, spec = request(f)
    compact(task_id, objects)
    result = BatchResult("request", text="paid answer", batch_id="paid-batch")
    batch_results.checkpoint(task_id, [result], [])
    assert batch_results.unconsumed(task_id)[0].request == spec
    objects.client.objects.clear()
    with pytest.raises(PayloadUnavailable):
        batch_results.unconsumed(task_id)
    receipt = db.query_one("SELECT * FROM batch_result_receipts WHERE task_id=%s", (task_id,))
    assert receipt["consumed_at"] is None and receipt["outcome"] is None
    with pytest.raises(PayloadUnavailable):
        batch_results.snapshot_specs(task_id, [replace(spec, input="replacement")])


def test_consumed_result_replay_does_not_read_missing_history(f, objects):
    task_id, _ = request(f)
    result = BatchResult("request", text="answer", batch_id="paid")
    batch_results.checkpoint(task_id, [result], [])
    with batch_results.consume_result(task_id, result) as receipt:
        receipt.outcome = "written"
    compact(task_id, objects)
    objects.client.objects.clear()
    assert batch_results.unconsumed(task_id) == []
    assert batch_results.progress_counts(task_id) == (1, 1)


@pytest.mark.parametrize("failure", ["put", "get", "corrupt"])
def test_unverified_object_never_clears_inline(f, objects, failure):
    task_id, _ = request(f)
    original = row(task_id)
    if failure == "put":
        objects.client.fail_put = True
    elif failure == "get":
        objects.client.fail_get = True
    else:
        objects.client.after_put = lambda: objects.client.objects.update(
            {key: b"corrupt" for key in objects.client.objects}
        )
    with pytest.raises(PayloadUnavailable):
        snapshot_payloads.migrate(original, objects, mode="copy")
    assert row(task_id) == original


@pytest.mark.parametrize("change", ["reactivate", "new_receipt", "source", "profile"])
def test_copy_rechecks_concurrent_changes(f, objects, change):
    task_id, _ = request(f)
    original = row(task_id)

    def mutate():
        if change == "reactivate":
            db.execute("UPDATE tasks SET status='pending' WHERE id=%s", (task_id,))
        elif change == "new_receipt":
            batch_results.checkpoint(task_id, [BatchResult("request", batch_id="paid")], [])
        elif change == "source":
            db.execute(
                "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s",
                (db.jsonb({"input": "different"}), task_id),
            )
        else:
            db.execute("UPDATE tasks SET kind='classify_job_profiles' WHERE id=%s", (task_id,))

    objects.client.after_put = mutate
    assert snapshot_payloads.migrate(original, objects, mode="copy") == "changed"
    assert row(task_id)["snapshot_ref"] is None
    assert row(task_id)["snapshot"] is not None


@pytest.mark.parametrize(
    "status,kind",
    [
        ("running", "verify_new"),
        ("awaiting_batch", "verify_new"),
        ("failed", "verify_new"),
        ("done", "classify_job_profiles"),
    ],
)
def test_excluded_populations_cannot_copy(f, objects, status, kind):
    task_id, _ = request(f, status=status, kind=kind)
    assert snapshot_payloads.candidates(after=None, limit=1, mode="copy") == []
    assert snapshot_payloads.migrate(row(task_id), objects, mode="copy") == "changed"
    assert objects.client.objects == {}


def test_nested_transaction_refuses_object_io(f, objects):
    task_id, _ = request(f)
    compact(task_id, objects)
    objects.client.fail_get = True
    with db.transaction(), pytest.raises(RuntimeError, match="transaction"):
        request_snapshots.resolve(row(task_id))
    with db.transaction(), pytest.raises(RuntimeError, match="transaction"):
        snapshot_payloads.migrate(row(task_id), objects, mode="restore")


def test_legacy_unknown_is_distinct_from_invalid_external(f, objects):
    task_id, _ = request(f)
    db.execute("UPDATE batch_requests SET snapshot=NULL WHERE task_id=%s", (task_id,))
    assert request_snapshots.resolve(row(task_id)) is None
    ref = objects.put_verified({"custom_id": "wrong-identity"})
    db.execute(
        "UPDATE batch_requests SET snapshot_ref=%s WHERE task_id=%s",
        (db.jsonb(asdict(ref)), task_id),
    )
    with pytest.raises(PayloadUnavailable, match="invalid"):
        request_snapshots.resolve(row(task_id))


def test_orphan_copy_is_idempotent_and_keyset_is_bounded(f, objects):
    first, _ = request(f)
    second, _ = request(f)
    original = row(first)
    objects.put_verified(original["snapshot"])
    assert snapshot_payloads.migrate(original, objects, mode="copy") == "copied"
    assert snapshot_payloads.migrate(row(first), objects, mode="copy") == "verified"
    assert len(objects.client.objects) == 1
    page = snapshot_payloads.candidates(after=None, limit=1, mode="copy")
    assert page[0]["task_id"] == first
    assert (
        snapshot_payloads.candidates(after=(first, "request"), limit=1, mode="copy")[0]["task_id"]
        == second
    )


def test_compaction_rechecks_unchanged_inline_and_reference(f, objects):
    task_id, _ = request(f)
    snapshot_payloads.migrate(row(task_id), objects, mode="copy")
    source = row(task_id)
    db.execute("UPDATE batch_requests SET snapshot_ref=NULL WHERE task_id=%s", (task_id,))
    assert snapshot_payloads.migrate(source, objects, mode="compact") == "changed"
    assert row(task_id)["snapshot"] is not None


def test_profile_historical_proof_hydrates_outside_transaction(f, objects):
    from api import review_gate
    from core.pool import in_transaction
    from tests.test_review_gate import proven_job

    job, task_id = proven_job(f)
    source = row(task_id)
    ref = objects.put_verified(source["snapshot"])
    db.execute(
        "UPDATE batch_requests SET snapshot=NULL,snapshot_ref=%s WHERE task_id=%s",
        (db.jsonb(asdict(ref)), task_id),
    )
    original_get = objects.client.get_object

    def outside(**kwargs):
        assert not in_transaction()
        return original_get(**kwargs)

    objects.client.get_object = outside
    evidence = review_gate.proven_profiles([job], {job["url"]: "exact posting content"}, 5000)
    assert job["url"] in evidence
    objects.client.objects.clear()
    with pytest.raises(PayloadUnavailable):
        review_gate.proven_profiles([job], {job["url"]: "exact posting content"}, 5000)


def test_cli_requires_backup_and_stops_before_unavailable_cursor(f, objects, monkeypatch, capsys):
    from types import SimpleNamespace

    from api.ai import migrate_snapshot_payloads

    monkeypatch.setattr(migrate_snapshot_payloads, "os", SimpleNamespace(environ={}))
    monkeypatch.setattr("sys.argv", ["migration", "compact", "--limit", "1"])
    with pytest.raises(SystemExit) as exit_code:
        migrate_snapshot_payloads.main()
    assert exit_code.value.code == 2
    task_id, _ = request(f)
    snapshot_payloads.migrate(row(task_id), objects, mode="copy")
    objects.client.objects.clear()
    monkeypatch.setattr("sys.argv", ["migration", "verify", "--limit", "1"])
    # The test harness owns the shared pool; the deployed CLI owns its own.
    monkeypatch.setattr("core.pool.pool.close", lambda: None)
    assert migrate_snapshot_payloads.main() == 1
    import json

    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["after"] is None
    assert report["counts"] == {"unavailable": 1}


def test_bulk_failure_commits_only_prefix_and_retry_closes_gap(f, objects):
    tasks = [request(f)[0] for _ in range(4)]
    rows = [row(tid) for tid in tasks]
    original = objects.put_verified
    failed = tasks[1]

    def put(value):
        if value["context"]["generation"] == failed:
            raise PayloadUnavailable("missing")
        return original(value)

    for tid in tasks:
        db.execute(
            "UPDATE batch_requests SET snapshot=jsonb_set(snapshot,'{context,generation}',%s) WHERE task_id=%s",
            (db.jsonb(tid), tid),
        )
    rows = [row(tid) for tid in tasks]
    objects.put_verified = put
    assert snapshot_payloads.migrate_many(rows, objects, mode="copy", workers=3) == [
        "copied",
        "unavailable",
    ]
    assert row(tasks[0])["snapshot_ref"] is not None
    assert all(row(tid)["snapshot_ref"] is None for tid in tasks[1:])
    objects.put_verified = original
    assert (
        snapshot_payloads.migrate_many(rows[1:], objects, mode="copy", workers=3) == ["copied"] * 3
    )


@pytest.mark.parametrize("size", [100, 1000])
def test_bulk_workload_statement_count_and_concurrency(f, monkeypatch, capsys, size):
    import json
    import threading
    import time

    from core.pool import in_transaction

    task_id = f.make_task("verify_new", {}, status="done")
    specs = [BatchSpec(str(index), "rules", "page " + str(index)) for index in range(size)]
    batch_results.snapshot_specs(task_id, specs)

    class MeasuredClient(ObjectClient):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0

        def get_object(self, **kwargs):
            assert not in_transaction()
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                # Simulated I/O releases the interpreter to exercise the pool;
                # elapsed time is reported, never used as a performance gate.
                time.sleep(0.001)
                return super().get_object(**kwargs)
            finally:
                with self.lock:
                    self.active -= 1

    store = PayloadStore(MeasuredClient(), "test-payloads")
    calls = []
    for name in ("query", "execute", "execute_count"):
        original = getattr(db, name)

        def counted(*args, _original=original, **kwargs):
            calls.append(args[0])
            return _original(*args, **kwargs)

        monkeypatch.setattr(db, name, counted)
    started = time.monotonic()
    after = None
    for _ in range(size // 100):
        sources = snapshot_payloads.candidates(after=after, limit=100, mode="copy")
        assert (
            snapshot_payloads.migrate_many(sources, store, mode="copy", workers=4)
            == ["copied"] * 100
        )
        after = (sources[-1]["task_id"], sources[-1]["custom_id"])
    assert len(calls) == 10 * (size // 100)
    assert 1 < store.client.peak <= 4
    with capsys.disabled():
        print(
            json.dumps(
                {
                    "snapshot_workload": size,
                    "chunk_size": 100,
                    "workers": 4,
                    "db_helper_statements_excluding_transaction_control": len(calls),
                    "observed_object_concurrency": store.client.peak,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
        )
