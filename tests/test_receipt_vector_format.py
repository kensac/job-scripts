"""Receipt vectors in version 1 gzip objects are written again as version 2,
and a second run finds nothing left."""

from __future__ import annotations

import asyncio
import gzip
import hashlib

from api import db
from api.ai import batch_results
from core.batch import BatchResult, BatchSpec
from core.payload_objects import PayloadStore, encode_payload
from tasks import receipt_vector_format

VECTORS = [[0.125, -1.25], [0.0, 4.0]]


def _receipt(f, custom_id: str, *, v1: bool, vectors=VECTORS) -> int:
    task_id = f.make_task("embed_postings_batch", {}, status="done")
    batch_results.snapshot_specs(task_id, [BatchSpec(custom_id, endpoint="/v1/embeddings")])
    batch_results.checkpoint(
        task_id,
        [BatchResult(custom_id, batch_id=f"batch-{task_id}", embedding_vectors=vectors)],
        [],
    )
    if v1:
        # The shape the first backfill wrote: gzip, digest of the plain bytes.
        store = PayloadStore.from_env()
        raw = encode_payload(vectors)
        digest = hashlib.sha256(raw).hexdigest()
        key = f"payloads/v1/sha256/{digest}.json.gz"
        store.client.objects[store.bucket, key] = gzip.compress(raw, mtime=0)
        ref = {"bucket": store.bucket, "key": key, "sha256": digest, "size": len(raw), "version": 1}
        db.execute(
            "UPDATE batch_result_receipts "
            "SET response = jsonb_set(response, '{embedding_vectors_ref}', %s) WHERE task_id = %s",
            (db.jsonb(ref), task_id),
        )
    return task_id


def _run(f) -> dict:
    task_id = f.make_task(receipt_vector_format.KIND, status="running")
    asyncio.run(receipt_vector_format.handle_rewrite_receipt_vectors_v1(task_id, {}))
    return db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]


def _response(task_id: int) -> dict:
    return db.query_one(
        "SELECT response FROM batch_result_receipts WHERE task_id = %s", (task_id,)
    )["response"]


def test_every_v1_reference_becomes_v2_and_reads_the_same_vectors(f, monkeypatch):
    monkeypatch.setattr(receipt_vector_format, "BATCH", 2)
    old = [_receipt(f, f"r{i}", v1=True) for i in range(5)]
    current = _receipt(f, "current", v1=False)
    untouched = _response(current)

    progress = _run(f)

    assert progress["total"] == 5 and progress["rewritten"] == 5
    assert progress["unavailable"] == 0
    for task_id in old:
        response = _response(task_id)
        assert response["embedding_vectors_ref"]["version"] == 2
        assert batch_results.response_payload(response)["embedding_vectors"] == VECTORS
    assert _response(current) == untouched
    assert receipt_vector_format.remaining() == 0
    assert _run(f)["total"] == 0


def test_an_unavailable_object_is_counted_and_left_for_the_next_run(f):
    # Its own vectors, so its object is not the one the other receipt names.
    missing = _receipt(f, "missing", v1=True, vectors=[[9.0]])
    PayloadStore.from_env().client.objects.clear()
    _receipt(f, "fine", v1=True)

    progress = _run(f)

    assert progress["rewritten"] == 1 and progress["unavailable"] == 1
    assert _response(missing)["embedding_vectors_ref"]["version"] == 1
    assert receipt_vector_format.remaining() == 1
