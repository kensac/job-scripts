import hashlib
import json
from types import SimpleNamespace

import pytest

from api import batch_results, db, worker
from api.tasks import embeddings, runtime
from core import batch
from core.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL


def _spec():
    return batch.BatchSpec("packed", inputs=["first", "second"], endpoint="/v1/embeddings")


def test_embedding_request_preserves_packing_and_omits_response_parameters():
    line = batch._build_line(_spec(), EMBEDDING_MODEL, "high", 6000)
    assert line == {
        "custom_id": "packed",
        "method": "POST",
        "url": "/v1/embeddings",
        "body": {
            "model": EMBEDDING_MODEL,
            "input": ["first", "second"],
            "encoding_format": "float",
        },
    }


@pytest.mark.asyncio
async def test_embedding_collection_reorders_indices_and_checkpoints_vectors(f):
    task_id = f.make_task("embed_postings_batch", {})
    batch_results.snapshot_specs(task_id, [_spec()])
    provider_batch = SimpleNamespace(
        id="packed",
        endpoint="/v1/embeddings",
        status="completed",
        output_file_id="out",
        error_file_id=None,
    )

    async def content(file_id):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "custom_id": "packed",
                    "response": {
                        "status_code": 200,
                        "body": {
                            "data": [
                                {"index": 1, "embedding": [0.2]},
                                {"index": 0, "embedding": [0.1]},
                            ],
                            "usage": {"prompt_tokens": 101, "total_tokens": 101},
                        },
                    },
                }
            )
        )

    results = await batch._collect_batch(
        SimpleNamespace(files=SimpleNamespace(content=content)),
        provider_batch,
        {},
        create_missing=True,
    )
    batch_results.checkpoint(task_id, list(results.values()), [])
    restored = batch_results.unconsumed(task_id)[0]
    assert restored.embedding_vectors == [[0.1], [0.2]]
    assert restored.usage == {"input_tokens": 101, "output_tokens": 0, "total_tokens": 101}
    assert restored.request.inputs == ["first", "second"]


def _receipt(f):
    originals = []
    for i in range(2):
        url = f"https://embedding.test/{i}"
        f.make_verdict(url, "content", content="a sufficiently detailed posting " * 30)
        row = db.query_one("SELECT id,input_content FROM ai_queries WHERE url=%s", (url,))
        originals.append(
            {
                "url": url,
                "content_row_id": row["id"],
                "content_hash": hashlib.sha256(row["input_content"].encode()).hexdigest(),
            }
        )
    task_id = f.make_task("embed_postings_batch", {}, status="running")
    spec = batch.BatchSpec(
        "packed",
        inputs=["first", "second"],
        endpoint="/v1/embeddings",
        context={"dimensions": EMBEDDING_DIMENSIONS, "rows": originals},
    )
    batch_results.snapshot_specs(task_id, [spec])
    result = batch.BatchResult(
        "packed",
        embedding_vectors=[[0.1] * EMBEDDING_DIMENSIONS] * 2,
        usage={"input_tokens": 101, "output_tokens": 0, "total_tokens": 101},
        model=EMBEDDING_MODEL,
        batch_id="packed",
    )
    batch_results.checkpoint(task_id, [result], [])
    return task_id, originals


@pytest.mark.asyncio
async def test_packed_resume_and_replay_without_key_preserve_vectors(f, monkeypatch):
    task_id, _originals = _receipt(f)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    await embeddings.handle_embed_postings_batch(task_id, {})
    rows = db.query("SELECT url,created_at,input_tokens FROM job_embeddings ORDER BY url")
    assert len(rows) == 2
    assert [row["input_tokens"] for row in rows] == [50, 50]
    await embeddings.handle_embed_postings_batch(task_id, {})
    assert db.query("SELECT url,created_at,input_tokens FROM job_embeddings ORDER BY url") == rows
    assert batch_results.progress_counts(task_id) == (1, 1)


@pytest.mark.asyncio
async def test_packed_result_keeps_current_sibling_when_one_page_changes(f):
    task_id, originals = _receipt(f)
    f.make_verdict(originals[0]["url"], "content", content="a changed detailed posting " * 30)
    await embeddings.handle_embed_postings_batch(task_id, {})
    assert [row["url"] for row in db.query("SELECT url FROM job_embeddings")] == [
        originals[1]["url"]
    ]


@pytest.mark.asyncio
async def test_packed_write_and_receipt_rollback_together(f, monkeypatch):
    task_id, _ = _receipt(f)
    store = embeddings._store

    def crash(rows):
        store(rows)
        raise RuntimeError("before acknowledgement")

    monkeypatch.setattr(embeddings, "_store", crash)
    with pytest.raises(RuntimeError, match="before acknowledgement"):
        await embeddings.handle_embed_postings_batch(task_id, {})
    assert db.query_one("SELECT count(*) AS n FROM job_embeddings")["n"] == 0
    assert len(batch_results.unconsumed(task_id)) == 1


def test_older_worker_cannot_claim_embedding_batches(f, monkeypatch):
    task_id = f.make_task("embed_postings_batch", {})
    monkeypatch.setattr(worker, "HANDLERS", {"embed_postings": embeddings.handle_embed_postings})
    assert worker._claim_task() is None
    assert db.query_one("SELECT status FROM tasks WHERE id=%s", (task_id,))["status"] == "pending"


@pytest.mark.asyncio
async def test_legacy_task_only_queues_new_kind(f, monkeypatch):
    task_id = f.make_task("embed_postings", {"cycle": 42})
    await embeddings.handle_embed_postings(task_id, {"cycle": 42})
    await embeddings.handle_embed_postings(task_id, {"cycle": 42})
    rows = db.query("SELECT kind,payload FROM tasks WHERE id<>%s", (task_id,))
    assert rows == [{"kind": "embed_postings_batch", "payload": {"cycle": 42}}]


@pytest.mark.asyncio
async def test_submission_parks_packed_requests_and_respects_existing_work(f, monkeypatch):
    for i in range(101):
        f.make_verdict(f"https://packed.test/{i}", "content", content="a detailed posting " * 30)
    task_id = f.make_task("embed_postings_batch", {}, status="running")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    submitted = []

    async def submit(specs, model, effort, max_output, on_event=None):
        submitted.extend(specs)
        on_event("batch", "submitted", {"requests": len(specs)})
        return ["batch"]

    monkeypatch.setattr(batch, "submit_responses_batches", submit)
    with pytest.raises(runtime.AwaitingBatch):
        await embeddings.handle_embed_postings_batch(task_id, {})
    assert [len(spec.inputs) for spec in submitted] == [100, 1]
    later = f.make_task("embed_postings_batch", {}, status="running")
    await embeddings.handle_embed_postings_batch(later, {})
    assert len(submitted) == 2


def test_responses_snapshots_and_receipts_retain_legacy_shape(f):
    task_id = f.make_task("extract_comp", {})
    batch_results.snapshot_specs(task_id, [batch.BatchSpec("url", "rules", "page", "Reply", {})])
    batch_results.checkpoint(
        task_id, [batch.BatchResult("url", text="answer", batch_id="responses")], []
    )
    snapshot = db.query_one("SELECT snapshot FROM batch_requests")["snapshot"]
    assert "endpoint" not in snapshot and "inputs" not in snapshot
    assert (
        "embedding_vectors"
        not in db.query_one("SELECT response FROM batch_result_receipts")["response"]
    )


def test_embedding_input_limit_applies_across_packed_requests(monkeypatch):
    monkeypatch.setattr(batch, "BATCH_MAX_REQUESTS", 3)
    assert [len(chunk) for chunk in batch._chunk_specs([_spec(), _spec()], 0)] == [1, 1]


@pytest.mark.asyncio
async def test_missing_provider_usage_remains_unknown(f):
    task_id, _ = _receipt(f)
    db.execute(
        "UPDATE batch_result_receipts SET response=response || '{\"usage\":null}'::jsonb WHERE task_id=%s",
        (task_id,),
    )
    await embeddings.handle_embed_postings_batch(task_id, {})
    assert db.query("SELECT input_tokens,cost_usd FROM job_embeddings") == [
        {"input_tokens": None, "cost_usd": None},
        {"input_tokens": None, "cost_usd": None},
    ]


@pytest.mark.asyncio
async def test_rejected_packed_request_is_acknowledged_without_live_fallback(f, monkeypatch):
    task_id, _ = _receipt(f)
    db.execute(
        'UPDATE batch_result_receipts SET response=response || \'{"error":"input too long"}\'::jsonb WHERE task_id=%s',
        (task_id,),
    )

    async def no_submit(*args, **kwargs):
        raise AssertionError("unexpected provider retry")

    monkeypatch.setattr(batch, "submit_responses_batches", no_submit)
    await embeddings.handle_embed_postings_batch(task_id, {})
    assert batch_results.outcome_counts(task_id) == {"failed": 1}
    assert db.query_one("SELECT count(*) AS n FROM job_embeddings")["n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["numeric_bool", "index_bool", "non_record", "missing_model"])
async def test_malformed_provider_vectors_are_acknowledged_without_database_failure(f, malformed):
    task_id, _ = _receipt(f)
    db.execute("DELETE FROM batch_result_receipts WHERE task_id=%s", (task_id,))
    data = [{"index": i, "embedding": [0.1] * EMBEDDING_DIMENSIONS} for i in range(2)]
    if malformed == "numeric_bool":
        for item in data:
            item["embedding"][0] = True
    elif malformed == "index_bool":
        data[0]["index"], data[1]["index"] = False, True
    elif malformed == "non_record":
        data[0] = "not a record"
    provider_batch = SimpleNamespace(
        id="packed",
        endpoint="/v1/embeddings",
        status="completed",
        output_file_id="out",
        error_file_id=None,
    )

    async def content(file_id):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "custom_id": "packed",
                    "response": {
                        "status_code": 200,
                        "body": {
                            "data": data,
                            "usage": {"prompt_tokens": 101, "total_tokens": 101},
                        },
                    },
                }
            )
        )

    results = await batch._collect_batch(
        SimpleNamespace(files=SimpleNamespace(content=content)),
        provider_batch,
        {},
        create_missing=True,
    )
    for result in results.values():
        result.model = None if malformed == "missing_model" else EMBEDDING_MODEL
    batch_results.checkpoint(task_id, list(results.values()), [])
    await embeddings.handle_embed_postings_batch(task_id, {})
    assert db.query_one("SELECT count(*) AS n FROM job_embeddings")["n"] == 0
    assert batch_results.unconsumed(task_id) == []
    expected = {
        "numeric_bool": "discarded",
        "index_bool": "failed",
        "non_record": "failed",
        "missing_model": "unknown_model",
    }
    assert batch_results.outcome_counts(task_id) == {expected[malformed]: 1}
