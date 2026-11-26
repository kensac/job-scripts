"""Embeds postings with the model the fleet serves itself.

The same postings as the OpenAI sweep (tasks.derivations.embeddings): every
posting some person can see whose current page has no vector, or a vector
from an older page. An unchanged re-scrape is re-stamped (tasks.rescrape).
Each run takes up to `local_embed_per_cycle` of them and embeds them on the
servers this host names (core.derivations.local_embeddings), concurrently
up to the slots each server runs; only a host with servers claims the kind.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from api import db
from api.board import visibility
from core import page_fetches
from core.derivations.embeddings import EMBEDDING_INPUT_CHARS
from core.derivations.local_embeddings import (
    LOCAL_EMBEDDING_MODEL,
    EmbeddingUnavailable,
    embed_document,
    server_urls,
)
from tasks import rescrape
from tasks.derivations.embeddings import candidate_sql
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

KIND = "embed_postings_local"

CANDIDATES = candidate_sql(visibility.across_users("j.url"), "posting_embeddings")

# Requests in flight per server: the slots llama-server is started with
# (`-np`, deploy/embeddings in optiplex-config), so every slot stays busy.
SLOTS_PER_SERVER = 4

# Postings written per statement, so a run that stops part way keeps what it
# embedded and progress moves as it goes.
_WRITE_EVERY = 50

_sessions = threading.local()


def _session() -> requests.Session:
    session = getattr(_sessions, "session", None)
    if session is None:
        session = _sessions.session = requests.Session()
    return session


def _store(rows: list[dict[str, Any]]) -> int:
    """Upserts vectors, never over one computed from a newer page, the guard
    job_embeddings' writer keeps (tasks.derivations.embeddings._store)."""
    return db.execute_count(
        """
        INSERT INTO posting_embeddings (url, embedding, model, content_hash, content_row_id)
        SELECT r.url, r.embedding::halfvec, r.model, r.hash, r.row_id
        FROM jsonb_to_recordset(%s) AS r(
            url text, embedding text, model text, hash text, row_id bigint
        )
        ON CONFLICT (url) DO UPDATE SET
            embedding = EXCLUDED.embedding, model = EXCLUDED.model,
            content_hash = EXCLUDED.content_hash,
            content_row_id = EXCLUDED.content_row_id,
            created_at = now()
        WHERE posting_embeddings.content_row_id IS NULL
           OR posting_embeddings.content_row_id <= EXCLUDED.content_row_id
           OR NOT EXISTS (
               SELECT 1 FROM page_texts p WHERE p.id = posting_embeddings.content_row_id
           )
        """,
        (db.jsonb(rows),),
    )


def candidates(cap: int) -> list[dict[str, Any]]:
    rows = page_fetches.resolve(db.query(CANDIDATES, {"cap": cap}))
    return rescrape.drop_unchanged(rows, table="posting_embeddings", limit=EMBEDDING_INPUT_CHARS)


def _embed(job: tuple[str, dict[str, Any]]) -> dict[str, Any] | None:
    url, row = job
    try:
        embedded = embed_document(_session(), url, row["input_content"])
    except EmbeddingUnavailable:
        logger.warning("embedding failed for %s on %s", row["url"], url, exc_info=True)
        return None
    return {
        "url": row["url"],
        "embedding": str(embedded.vector),
        "model": LOCAL_EMBEDDING_MODEL,
        "hash": row["content_hash"],
        "row_id": row["content_row_id"],
    }


def run(task_id: int, cap: int) -> dict[str, int]:
    urls = server_urls()
    if not urls:
        raise EmbeddingUnavailable("no embedding server configured on this host")
    rows = candidates(cap)
    counts = {"selected": len(rows), "written": 0, "failed": 0}
    pending: list[dict[str, Any]] = []
    jobs = [(urls[i % len(urls)], row) for i, row in enumerate(rows)]
    with ThreadPoolExecutor(max_workers=SLOTS_PER_SERVER * len(urls)) as executor:
        for done, embedded in enumerate(executor.map(_embed, jobs), start=1):
            if embedded is None:
                counts["failed"] += 1
            else:
                pending.append(embedded)
            if len(pending) >= _WRITE_EVERY or done == len(jobs):
                counts["written"] += _store(pending) if pending else 0
                pending = []
                set_progress(task_id, done, len(jobs), f"embedded {counts['written']}")
            if cancelled(task_id):
                executor.shutdown(wait=False, cancel_futures=True)
                break
    if pending:
        counts["written"] += _store(pending)
    return counts


async def handle_embed_postings_local(task_id: int, payload: dict[str, Any]) -> None:
    if not db.get_config("local_embedding_enabled"):
        set_progress(task_id, 0, 0, "switched off")
        return
    cap = int(db.get_config("local_embed_per_cycle"))
    counts = await asyncio.to_thread(run, task_id, cap)
    set_progress(
        task_id,
        counts["written"],
        counts["selected"],
        f"embedded {counts['written']} of {counts['selected']}, {counts['failed']} failed",
        extra=counts,
    )
    logger.info("%s: %s", KIND, counts)
