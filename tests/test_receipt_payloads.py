from dataclasses import asdict, replace

import pytest

from api import db
from api.ai import batch_results
from core.batch import BatchResult, BatchSpec
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
from tests.factories import ObjectClient


@pytest.fixture
def objects():
    return PayloadStore(ObjectClient(), "test-payloads")


def receipt(f):
    task_id = f.make_task("embed_postings_batch", {}, status="done")
    batch_results.snapshot_specs(task_id, [BatchSpec("packed", endpoint="/v1/embeddings")])
    result = BatchResult(
        "packed",
        batch_id=f"batch-{task_id}",
        embedding_vectors=[[0.1, 0.2], [0.3, 0.4]],
        usage={"input_tokens": 10, "output_tokens": 0},
        model="embedding-model",
    )
    batch_results.checkpoint(task_id, [result], [])
    return task_id, result


def row(task_id):
    return db.query_one("SELECT * FROM batch_result_receipts WHERE task_id=%s", (task_id,))


def test_verified_object_round_trip_and_deterministic_identity(objects):
    value = [[0.125, -1.25], [0, 4]]
    ref = objects.put_verified(value)
    assert objects.get(ref) == value
    assert objects.put_verified(value) == ref
    assert len(objects.client.objects) == 1
    assert ref.version == 2
    assert objects.client.objects[ref.bucket, ref.key] == b"[[0.125,-1.25],[0,4]]"
    assert ref.size == len(objects.client.objects[ref.bucket, ref.key])


def test_a_version_1_reference_is_refused(objects):
    ref = objects.put_verified([[0.5]])
    v1 = {**asdict(ref), "version": 1, "key": ref.key.replace("/v2/", "/v1/") + ".gz"}
    with pytest.raises(PayloadUnavailable):
        PayloadRef.parse(v1)


@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "wrong_digest", "wrong_size", "bad_version"]
)
def test_object_integrity_failures_are_explicit(objects, failure):
    ref = objects.put_verified([[0.5]])
    if failure == "missing":
        objects.client.objects.clear()
    elif failure == "corrupt":
        objects.client.objects[ref.bucket, ref.key] = b"[[0.6]]"
    elif failure == "wrong_digest":
        ref = replace(ref, sha256="0" * 64)
    elif failure == "wrong_size":
        ref = replace(ref, size=ref.size + 1)
    else:
        ref = replace(ref, version=3)
    with pytest.raises(PayloadUnavailable):
        objects.get(ref)


def test_reference_parse_rejects_wrong_types():
    with pytest.raises(PayloadUnavailable):
        PayloadRef.parse({"bucket": "b", "key": "k", "sha256": "x", "size": True, "version": 1})


def test_store_rejects_empty_credentials_before_sdk_discovery(monkeypatch):
    for name in ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
        monkeypatch.setenv("JOBTRACKER_S3_" + name, "")
    with pytest.raises(PayloadUnavailable, match="configuration"):
        PayloadStore.from_env()


def test_receipt_holds_only_the_reference(f):
    task_id, result = receipt(f)
    response = row(task_id)["response"]
    assert "embedding_vectors" not in response
    assert batch_results.response_payload(response)["embedding_vectors"] == (
        result.embedding_vectors
    )


def test_required_receipt_loading_raises_without_acknowledging(f):
    task_id, result = receipt(f)
    assert batch_results.unconsumed(task_id)[0].embedding_vectors == result.embedding_vectors
    PayloadStore.from_env().client.objects.clear()
    with pytest.raises(PayloadUnavailable):
        batch_results.unconsumed(task_id)
    assert row(task_id)["outcome"] is None
    assert row(task_id)["consumed_at"] is None
