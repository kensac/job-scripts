import threading
import time
from dataclasses import replace

import pytest

from api import db, worker
from api.ai import batch_results, request_snapshots
from core import batch
from core.batch import BatchSpec
from core.payload_objects import MAX_CONNECTIONS, PayloadRef, PayloadStore
from tasks import runtime
from tests.factories import ObjectClient


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    return store


def rows(task_id):
    return db.query("SELECT * FROM batch_requests WHERE task_id=%s ORDER BY custom_id", (task_id,))


SPECS = [
    BatchSpec("a", "rules", "page a", "Reply", {"type": "object"}, context={"n": 1}),
    BatchSpec("b", endpoint="/v1/embeddings", inputs=["one", "two"]),
]


def test_new_requests_are_stored_only_as_verified_objects(f, objects):
    task_id = f.make_task("verify_new", {})
    reads = []
    original_get = objects.client.get_object
    objects.client.get_object = lambda **kw: reads.append(kw) or original_get(**kw)
    assert batch_results.snapshot_specs(task_id, SPECS) == SPECS
    # One read per object, the upload's own round-trip check: freezing reads nothing more.
    assert len(reads) == len(SPECS)
    stored = rows(task_id)
    assert [row["custom_id"] for row in stored] == ["a", "b"]
    for row, spec in zip(stored, SPECS, strict=True):
        assert row["snapshot"] is None
        ref = PayloadRef.parse(row["snapshot_ref"])
        assert ref.version == 2
        expected = {
            "custom_id": spec.custom_id,
            "instructions": spec.instructions,
            "input": spec.input,
            "schema_name": spec.schema_name,
            "schema": spec.schema,
            "context": spec.context,
        }
        if spec.endpoint != "/v1/responses":
            expected |= {"endpoint": spec.endpoint, "inputs": spec.inputs}
        assert objects.get(ref) == expected
        assert request_snapshots.resolve(row, objects) == spec


def test_existing_request_wins_without_new_upload(f, objects):
    task_id = f.make_task("verify_new", {})
    original = SPECS[0]
    batch_results.snapshot_specs(task_id, [original])
    before = rows(task_id)
    uploaded = dict(objects.client.objects)
    changed = replace(original, input="a different page")
    assert batch_results.snapshot_specs(task_id, [changed]) == [original]
    assert rows(task_id) == before
    assert objects.client.objects == uploaded


def test_legacy_inline_request_wins(f, objects):
    task_id = f.make_task("verify_new", {})
    original = SPECS[0]
    f.make_inline_request(task_id, original)
    before = rows(task_id)
    changed = replace(original, input="a different page")
    assert batch_results.snapshot_specs(task_id, [changed, SPECS[1]]) == [
        original,
        SPECS[1],
    ]
    after = rows(task_id)
    assert after[0] == before[0]
    assert after[1]["snapshot"] is None and after[1]["snapshot_ref"] is not None


def test_uploads_run_concurrently_within_the_connection_pool(f, objects):
    lock = threading.Lock()
    active, peak = [0], [0]
    original_put = objects.client.put_object

    def put(**kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.005)
            return original_put(**kwargs)
        finally:
            with lock:
                active[0] -= 1

    objects.client.put_object = put
    task_id = f.make_task("verify_new", {})
    specs = [BatchSpec(str(index), input=str(index)) for index in range(3 * MAX_CONNECTIONS)]
    assert batch_results.snapshot_specs(task_id, specs) == specs
    assert 1 < peak[0] <= MAX_CONNECTIONS


@pytest.mark.asyncio
async def test_storage_outage_submits_nothing_and_recovers(f, objects, monkeypatch):
    from tasks.runtime.payload_recovery import retry

    submitted = []

    async def submit(specs, *args, **kwargs):
        submitted.append(specs)
        return ["paid"]

    async def handler(task_id, payload):
        await runtime.submit_or_collect(task_id, SPECS, "model", "", 100, None)

    monkeypatch.setattr(batch, "submit_responses_batches", submit)
    monkeypatch.setitem(worker.HANDLERS, "test_kind", handler)
    task_id = runtime.enqueue("test_kind", {})
    objects.client.fail_put = True
    await worker.run_once()
    assert submitted == []
    assert rows(task_id) == []
    task = db.query_one("SELECT status,payload FROM tasks WHERE id=%s", (task_id,))
    assert task["status"] == "failed"
    assert task["payload"]["payload_recovery"] == {"reason": "payload_unavailable"}

    objects.client.fail_put = False
    assert retry(task_id, objects) == "pending"
    await worker.run_once()
    assert submitted == [SPECS]
    assert all(row["snapshot"] is None for row in rows(task_id))


def test_profile_requests_stay_inline_and_gate_reads_no_object(f, objects):
    from api import review_gate
    from tests.test_review_gate import proven_job

    calls = []
    for name in ("put_object", "get_object"):
        original = getattr(objects.client, name)
        setattr(
            objects.client,
            name,
            lambda _original=original, _name=name, **kw: calls.append(_name) or _original(**kw),
        )
    job, task_id = proven_job(f)
    stored = rows(task_id)
    assert len(stored) == 1
    assert stored[0]["snapshot"] is not None and stored[0]["snapshot_ref"] is None
    evidence = review_gate.proven_profiles([job], {job["url"]: "exact posting content"}, 1000)
    assert job["url"] in evidence
    assert calls == []
