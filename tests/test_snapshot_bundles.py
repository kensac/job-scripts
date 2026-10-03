"""Historical request snapshots stored as members of one per-task bundle object."""

import hashlib
from dataclasses import asdict

import pytest

from api import db
from api.ai import batch_results, request_snapshots, snapshot_payloads
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
    for spec in specs:
        f.make_inline_request(task_id, spec)
    return task_id, specs


def rows(task_id):
    return db.query("SELECT * FROM batch_requests WHERE task_id=%s ORDER BY custom_id", (task_id,))


def attach_bundle(store, sources, *, keep_inline=True):
    """Stand in for the bundle writer: one object, one member reference per row."""
    refs = store.put_bundle({source["custom_id"]: source["snapshot"] for source in sources})
    for source in sources:
        db.execute(
            "UPDATE batch_requests SET snapshot_ref=%s"
            + ("" if keep_inline else ",snapshot=NULL")
            + " WHERE task_id=%s AND custom_id=%s",
            (db.jsonb(asdict(refs[source["custom_id"]])), source["task_id"], source["custom_id"]),
        )
    return refs


def test_member_reference_shape_and_old_reader_refuses_it(f, objects):
    task_id, _ = task(f, 2)
    sources = rows(task_id)
    refs = attach_bundle(objects, sources)
    ref = asdict(refs["r0"])
    raw = encode_payload(sources[0]["snapshot"])
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
    inline = [request_snapshots.resolve(source) for source in rows(task_id)]
    attach_bundle(objects, rows(task_id), keep_inline=False)
    referenced = rows(task_id)
    assert all(source["snapshot"] is None for source in referenced)
    assert [request_snapshots.resolve(source) for source in referenced] == inline == specs


@pytest.mark.parametrize("tamper", ["bundle", "member_digest", "member_name", "member_size"])
def test_tampered_bundle_or_member_raises(f, objects, tamper):
    task_id, _ = task(f, 2)
    refs = attach_bundle(objects, rows(task_id), keep_inline=False)
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
    sources = rows(task_id)
    attach_bundle(objects, sources[:3], keep_inline=False)
    attach_bundle(objects, sources[3:], keep_inline=False)
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
    attach_bundle(objects, rows(task_id), keep_inline=False)
    objects.client.gets.clear()
    assert batch_results.snapshot_specs(task_id, specs) == specs
    assert len(objects.client.gets) == 1
    objects.client.gets.clear()
    assert payload_recovery.retry(task_id, objects) == "pending"
    assert len(objects.client.gets) == 1


def test_mixed_inline_v2_and_member_rows_in_one_task_resolve(f, objects):
    task_id, specs = task(f, 4)
    sources = rows(task_id)
    v2 = objects.put_verified(sources[1]["snapshot"])
    db.execute(
        "UPDATE batch_requests SET snapshot=NULL,snapshot_ref=%s WHERE task_id=%s AND custom_id='r1'",
        (db.jsonb(asdict(v2)), task_id),
    )
    attach_bundle(objects, sources[2:], keep_inline=False)
    mixed = rows(task_id)
    assert mixed[0]["snapshot_ref"] is None
    assert mixed[1]["snapshot_ref"]["version"] == 2
    assert mixed[2]["snapshot_ref"]["version"] == 3
    assert [request_snapshots.resolve(source) for source in mixed] == specs
    batch_results.checkpoint(
        task_id, [BatchResult(spec.custom_id, text="a", batch_id="paid") for spec in specs], []
    )
    assert [result.request for result in batch_results.unconsumed(task_id)] == specs


def test_compact_clears_only_verified_member_rows_and_restore_is_exact(f, objects):
    task_id, _ = task(f, 4)
    original = rows(task_id)
    attach_bundle(objects, original)
    # r2's inline value no longer matches its member: it and everything after
    # it in the chunk must keep their inline value.
    db.execute(
        "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s AND custom_id='r2'",
        (db.jsonb({"input": "edited"}), task_id),
    )
    objects.client.gets.clear()
    outcomes = snapshot_payloads.migrate_many(rows(task_id), objects, mode="compact", workers=3)
    assert outcomes == ["compacted", "compacted", "unavailable"]
    assert len(objects.client.gets) == 1
    after = rows(task_id)
    assert [source["snapshot"] is None for source in after] == [True, True, False, False]
    assert snapshot_payloads.migrate_many(after[:2], objects, mode="verify") == ["verified"] * 2
    assert snapshot_payloads.migrate_many(after[:2], objects, mode="restore") == ["restored"] * 2
    restored = rows(task_id)
    assert restored[:2] == [{**source, "snapshot_ref": None} for source in original[:2]]


def test_copy_leaves_member_rows_as_they_are(f, objects):
    task_id, _ = task(f, 2)
    attach_bundle(objects, rows(task_id))
    before = rows(task_id)
    puts = len(objects.client.objects)
    assert snapshot_payloads.migrate_many(before, objects, mode="copy") == ["verified"] * 2
    assert rows(task_id) == before
    assert len(objects.client.objects) == puts


def test_manifest_operates_on_member_references(f, objects):
    task_id, _ = task(f, 2)
    sources = rows(task_id)
    manifest = [
        {
            "task_id": task_id,
            "custom_id": source["custom_id"],
            "snapshot_sha256": hashlib.sha256(encode_payload(source["snapshot"])).hexdigest(),
        }
        for source in sources
    ]
    attach_bundle(objects, sources)
    for item, source in zip(manifest, rows(task_id), strict=True):
        item["reference"] = source["snapshot_ref"]
    logical = sum(len(encode_payload(source["snapshot"])) for source in sources)
    for mode in ("verify", "compact", "verify", "restore"):
        result = snapshot_payloads.migrate_manifest(
            manifest, objects, mode=mode, limit=2, backup_complete=True
        )
        assert result.exhausted and result.completed == 2
        assert result.logical_bytes_verified == logical
    assert [source["snapshot"] for source in rows(task_id)] == [
        source["snapshot"] for source in sources
    ]
