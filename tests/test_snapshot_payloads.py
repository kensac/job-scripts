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
