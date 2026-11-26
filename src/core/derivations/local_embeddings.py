"""Posting embeddings from a model served on the fleet's own hardware.

A host that runs the embedding sweep names the servers it uses
(llama.cpp's `llama-server --embedding`, OpenAI-compatible) in
JOBTRACKER_EMBEDDING_URLS, a comma list: its own GPU and CPU, and servers on
other machines on the tailnet, such as a laptop that serves only while its
owner is not using it. Only such a host claims `embed_postings_local`
(api.worker_lifecycle.claims.NEEDS_ENV). A server that cannot be reached is
ServerUnreachable, which the sweep treats as absent rather than as a failed
posting.

A posting is embedded whole: its text, up to EMBEDDING_INPUT_CHARS, is split
into chunks the model's window holds, each chunk is embedded, and the vectors
are averaged weighted by their token counts, then normalised. Nothing is cut
to fit the window.

Model choice, on a judged set of 800 production postings (2026-11-26): for
200 pairs of the same company and title listed by two different sources, how
often each posting's pair is among its ten nearest; and for 400 postings
with one filter's verdict (200 passed, 200 rejected), how many of each one's
ten nearest share its verdict.

    model                        pair recall@10   verdict agreement@10
    text-embedding-3-small            0.81               0.72
    nomic-embed-text-v1.5             0.92               0.72

On optiplex's Radeon R7 350 (Vulkan) and four CPU threads together it embedded
0.9 postings a second. What a posting is embedded as, without its company's
shared text, is core.derivations.company_text.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import requests

from core.derivations.embeddings import EMBEDDING_INPUT_CHARS
from core.env import env_list

# The model the servers load (nomic-embed-text-v1.5, GGUF Q8_0), recorded on
# every row so a later model's rows are told apart.
LOCAL_EMBEDDING_MODEL = "nomic-embed-text-v1.5"
LOCAL_EMBEDDING_DIMENSIONS = 768
# The model's training window is 2,048 tokens; a chunk leaves room for the
# task prefix it is embedded with.
CHUNK_TOKENS = 2000
# nomic-embed-text distinguishes documents from queries by a prefix it was
# trained with; every stored vector is a document.
DOCUMENT_PREFIX = "search_document: "

URLS_ENV = "JOBTRACKER_EMBEDDING_URLS"

# A long posting is a few chunks, each a second or two on the slowest server;
# this bounds one request, not the run.
_TIMEOUT_SECONDS = 300


class EmbeddingUnavailable(RuntimeError):
    pass


class ServerUnreachable(EmbeddingUnavailable):
    """The server is not there, or not ready: refused, reset, unresolvable,
    or still loading its model (503). Says nothing about the posting."""


def server_urls() -> list[str]:
    return [url.rstrip("/") for url in env_list(URLS_ENV)]


@dataclass(frozen=True)
class Embedded:
    vector: list[float]
    chunks: int


def _call(session: requests.Session, method: str, url: str, path: str, **kwargs: Any) -> dict:
    try:
        response = session.request(method, f"{url}{path}", timeout=_TIMEOUT_SECONDS, **kwargs)
        if response.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            raise ServerUnreachable(f"embedding server {url} is not ready")
        response.raise_for_status()
        return response.json()
    except requests.ConnectionError as exc:
        raise ServerUnreachable(f"embedding server {url} cannot be reached") from exc
    except (requests.RequestException, ValueError) as exc:
        raise EmbeddingUnavailable(f"embedding server {url}{path} failed") from exc


def _post(session: requests.Session, url: str, path: str, body: dict) -> dict:
    return _call(session, "POST", url, path, json=body)


def slots(session: requests.Session, url: str) -> int:
    """How many requests the server at `url` serves at once (llama-server's
    `-np`), as it reports it, so each machine sets its own."""
    total = _call(session, "GET", url, "/props").get("total_slots")
    if not isinstance(total, int) or total < 1:
        raise EmbeddingUnavailable(f"embedding server {url} reports no slots")
    return total


def embed_document(session: requests.Session, url: str, text: str) -> Embedded:
    """One posting's vector from the server at `url`: chunked to the model's
    window, chunk vectors averaged by token count, normalised."""
    text = text[:EMBEDDING_INPUT_CHARS]
    tokens: Sequence[int] = _post(session, url, "/tokenize", {"content": text})["tokens"]
    spans = [tokens[s : s + CHUNK_TOKENS] for s in range(0, len(tokens), CHUNK_TOKENS)] or [[]]
    chunks = [
        _post(session, url, "/detokenize", {"tokens": list(span)})["content"] if span else ""
        for span in spans
    ]
    data = _post(session, url, "/v1/embeddings", {"input": [DOCUMENT_PREFIX + c for c in chunks]})[
        "data"
    ]
    vectors = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
    if len(vectors) != len(chunks) or any(len(v) != LOCAL_EMBEDDING_DIMENSIONS for v in vectors):
        raise EmbeddingUnavailable("embedding server returned vectors of another shape")
    weights = [max(1, len(span)) for span in spans]
    mean = [
        sum(w * v[d] for w, v in zip(weights, vectors, strict=True))
        for d in range(LOCAL_EMBEDDING_DIMENSIONS)
    ]
    norm = math.sqrt(sum(x * x for x in mean))
    if not norm or not math.isfinite(norm):
        raise EmbeddingUnavailable("embedding server returned a zero or non-finite vector")
    return Embedded([x / norm for x in mean], len(chunks))
