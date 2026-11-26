"""Postings embedded with the model the fleet serves itself
(core.derivations.local_embeddings, tasks.derivations.local_embeddings)."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from api import db
from core.derivations import local_embeddings as core
from tasks.derivations import local_embeddings
from tests.factories import make_task

CONTENT = "a long job description " * 20
DIMS = core.LOCAL_EMBEDDING_DIMENSIONS


class FakeServer:
    """llama-server's /tokenize, /detokenize and /v1/embeddings: one token per
    word, and a vector whose first component is the chunk's token count."""

    def __init__(self):
        self.embedded: list[str] = []

    def post(self, url, json, timeout):
        path = url.split("/", 3)[-1]
        if path == "tokenize":
            body = {"tokens": list(range(len(json["content"].split())))}
        elif path == "detokenize":
            body = {"content": " ".join("w" for _ in json["tokens"])}
        else:
            self.embedded.extend(json["input"])
            body = {
                "data": [
                    {"index": i, "embedding": [float(len(t.split())), 1.0] + [0.0] * (DIMS - 2)}
                    for i, t in enumerate(json["input"])
                ]
            }
        return _Response(body)


class _Response:
    def __init__(self, body: Any):
        self.body: Any = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_a_long_posting_is_embedded_whole_in_weighted_chunks(monkeypatch):
    monkeypatch.setattr(core, "CHUNK_TOKENS", 10)
    server = FakeServer()
    embedded = core.embed_document(server, "http://emb", "word " * 25)

    assert embedded.chunks == 3
    assert all(text.startswith(core.DOCUMENT_PREFIX) for text in server.embedded)
    # Chunks of 10, 10 and 5 tokens, each embedded with the one-word prefix,
    # so the fake's first component reads 11, 11 and 6: weighted by the
    # chunks' token counts.
    first = (10 * 11 + 10 * 11 + 5 * 6) / 25
    norm = math.hypot(first, 1.0)
    assert embedded.vector[:2] == pytest.approx([first / norm, 1.0 / norm])


def test_a_vector_of_another_width_is_refused():
    class Narrow(FakeServer):
        def post(self, url, json, timeout):
            response = super().post(url, json, timeout)
            if url.endswith("/v1/embeddings"):
                response.body["data"][0]["embedding"] = [1.0, 2.0]
            return response

    with pytest.raises(core.EmbeddingUnavailable):
        core.embed_document(Narrow(), "http://emb", "a few words")


def test_the_column_width_is_the_models():
    row = db.query_one(
        "SELECT atttypmod AS dims FROM pg_attribute "
        "WHERE attrelid = 'posting_embeddings'::regclass AND attname = 'embedding'"
    )
    assert row == {"dims": DIMS}


def test_a_host_without_a_server_never_claims_the_kind():
    from api.worker_lifecycle import claims

    assert "embed_postings_local" in claims.EXCLUDE_KINDS


def _run(monkeypatch, server) -> int:
    monkeypatch.setenv(core.URLS_ENV, "http://emb-a,http://emb-b")
    monkeypatch.setattr(local_embeddings, "_session", lambda: server)
    task_id = make_task(local_embeddings.KIND, status="running")
    asyncio.run(local_embeddings.handle_embed_postings_local(task_id, {}))
    return task_id


def test_a_visible_posting_is_embedded_once_and_an_unchanged_rescrape_is_restamped(f, monkeypatch):
    job_id, url = f.make_ready_job(content=CONTENT)
    f.upload(job_id, f.make_user())
    server = FakeServer()
    _run(monkeypatch, server)

    row = db.query_one(
        "SELECT model, content_row_id FROM posting_embeddings WHERE url = %s", (url,)
    )
    assert row is not None and row["model"] == core.LOCAL_EMBEDDING_MODEL
    calls = len(server.embedded)

    f.make_fetch(url, content=CONTENT)
    _run(monkeypatch, server)
    assert len(server.embedded) == calls, "an unchanged page is not embedded again"
    restamped = db.query_one("SELECT content_row_id FROM posting_embeddings WHERE url = %s", (url,))
    assert restamped is not None and restamped["content_row_id"] > row["content_row_id"]


def test_a_failed_posting_is_left_for_the_next_run(f, monkeypatch):
    job_id, url = f.make_ready_job(content=CONTENT)
    f.upload(job_id, f.make_user())

    class Down(FakeServer):
        def post(self, url, json, timeout):
            raise core.requests.ConnectionError("down")

    task_id = _run(monkeypatch, Down())
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (url,)) is None
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["failed"] == 1


def test_switched_off_embeds_nothing(f, monkeypatch, set_config):
    job_id, url = f.make_ready_job(content=CONTENT)
    f.upload(job_id, f.make_user())
    set_config("local_embedding_enabled", False)
    _run(monkeypatch, FakeServer())
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (url,)) is None
