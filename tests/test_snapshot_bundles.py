"""Historical request snapshots stored as members of one per-task bundle object."""

import hashlib
from dataclasses import asdict

import pytest

from api import db
from api.ai import batch_results, request_snapshots
from core.batch import BatchResult, BatchSpec
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable, encode_payload
from tests.factories import ObjectClient


class CountingClient(ObjectClient):
    def __init__(self):
        super().__init__()
        self.gets = []

    def get_object(self, *, Bucket, Key):
        self.gets.append(Key)
        return super().get_object(Bucket=Bucket, Key=Key)


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(CountingClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda **_: store)
    return store


def task(f, count, *, status="done"):
    task_id = f.make_task("verify_new", {}, status=status)
    specs = [
        BatchSpec(f"r{index}", "rules", f"page {index}", context={"n": index})
        for index in range(count)
    ]
    return task_id, specs


def rows(task_id):
    return db.query("SELECT * FROM batch_requests WHERE task_id=%s ORDER BY custom_id", (task_id,))


def attach_bundle(store, task_id, specs):
    """Stand in for the writer: one object, one member reference per request."""
    refs = store.put_bundle({spec.custom_id: batch_results.snapshot_of(spec) for spec in specs})
    for spec in specs:
        db.execute(
            "INSERT INTO batch_requests (task_id, custom_id, snapshot_ref) VALUES (%s,%s,%s)",
            (task_id, spec.custom_id, db.jsonb(asdict(refs[spec.custom_id]))),
        )
    return refs


def test_member_reference_shape_and_old_reader_refuses_it(f, objects):
    task_id, specs = task(f, 2)
    refs = attach_bundle(objects, task_id, specs)
    ref = asdict(refs["r0"])
    raw = encode_payload(batch_results.snapshot_of(specs[0]))
    assert ref["version"] == 3
    assert ref["member"] == "r0"
    assert ref["member_sha256"] == hashlib.sha256(raw).hexdigest()
    assert ref["member_size"] == len(raw)
    assert ref["key"] == f"payloads/v3/sha256/{ref['sha256']}.json"
    assert asdict(refs["r1"])["key"] == ref["key"]
    # A v1/v2-only reader must fail closed on a member reference, never read
    # the bundle as if it were the snapshot.
    with pytest.raises(PayloadUnavailable):
        PayloadRef.parse(ref)


def test_member_reference_resolves_to_the_identical_spec(f, objects):
    task_id, specs = task(f, 3)
    attach_bundle(objects, task_id, specs)
    assert [request_snapshots.resolve(source) for source in rows(task_id)] == specs


@pytest.mark.parametrize("tamper", ["bundle", "member_digest", "member_name", "member_size"])
def test_tampered_bundle_or_member_raises(f, objects, tamper):
    task_id, specs = task(f, 2)
    refs = attach_bundle(objects, task_id, specs)
    source = rows(task_id)[0]
    ref = dict(source["snapshot_ref"])
    if tamper == "bundle":
        key = (objects.bucket, ref["key"])
        objects.client.objects[key] = objects.client.objects[key].replace(b"page 0", b"page 9")
    elif tamper == "member_digest":
        ref["member_sha256"] = refs["r1"].member_sha256
    elif tamper == "member_name":
        ref["member"] = "r1"
    else:
        ref["member_size"] += 1
    with pytest.raises(PayloadUnavailable):
        request_snapshots.resolve({**source, "snapshot_ref": ref})


def test_resolving_a_whole_task_gets_each_bundle_once(f, objects):
    task_id, specs = task(f, 5)
    attach_bundle(objects, task_id, specs[:3])
    attach_bundle(objects, task_id, specs[3:])
    batch_results.checkpoint(
        task_id, [BatchResult(spec.custom_id, text="a", batch_id="paid") for spec in specs], []
    )
    objects.client.gets.clear()
    assert [result.request for result in batch_results.unconsumed(task_id)] == specs
    assert len(objects.client.gets) == 2
    assert len(set(objects.client.gets)) == 2


def test_recovery_and_freeze_get_each_bundle_once(f, objects):
    from tasks.runtime import payload_recovery

    task_id, specs = task(f, 4, status="failed")
    db.execute(
        "UPDATE tasks SET payload=%s WHERE id=%s",
        (db.jsonb({"payload_recovery": {"reason": "payload_unavailable"}}), task_id),
    )
    attach_bundle(objects, task_id, specs)
    objects.client.gets.clear()
    assert batch_results.snapshot_specs(task_id, specs) == specs
    assert len(objects.client.gets) == 1
    objects.client.gets.clear()
    assert payload_recovery.retry(task_id, objects) == "pending"
    assert len(objects.client.gets) == 1
