"""Embeds each posting once, so the corpus can be asked what resembles what."""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from api import db
from api.tasks.runtime import _set_progress
from core.embeddings import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_INPUT_CHARS,
    EMBEDDING_MODEL,
)
from core.pricing import estimate_cost_usd
from core.store import CONTENT_LATERAL

logger = logging.getLogger("jobtracker_worker")


# Synchronous, not batched, and that is a deliberate exception to the rule that
# scheduled work parks on the Batch API at half price. The whole corpus costs
# $0.47 sync against $0.23 batched, and collecting the difference would mean
# making core/batch.py's BATCH_ENDPOINT per-spec - it is hardcoded to
# /v1/responses and embeddings are /v1/embeddings. That path is days old and
# has already produced one production defect, so twenty-four cents is not a
# reason to widen it. No worker is held for long either: an embeddings call
# returns in about a second, where a batch takes hours.
#
# Bounded per cycle so one pass cannot hold a worker indefinitely, and sized
# from what the pass actually does rather than picked round: 2,000 postings is
# 20 requests of EMBEDDING_BATCH_SIZE, and at roughly a second a request that
# is well under the HEARTBEAT_TIMEOUT_MINUTES window even if every request
# retries. The whole corpus drains in 11 hourly cycles.
EMBED_POSTINGS_PER_CYCLE = int(os.environ.get("JOBTRACKER_EMBED_POSTINGS_PER_CYCLE", "2000"))


# Same shape as the requirements sweep's candidate query, and identical in the
# part that matters: already-embedded urls are filtered out BEFORE the lateral
# runs, because input_content is TOASTed and joining first would detoast page
# text for rows about to be discarded.
_CANDIDATES = f"""
    SELECT c.url, q.input_content
    FROM (
        SELECT DISTINCT a.url FROM ai_queries a
        WHERE NOT EXISTS (SELECT 1 FROM job_embeddings e WHERE e.url = a.url)
    ) c
    {CONTENT_LATERAL.format(url="c.url")}
    LIMIT %(cap)s
"""


def _store(rows: list[dict[str, Any]]) -> None:
    """One statement per wave rather than per posting.

    ON CONFLICT rather than a plain insert so a wave that is retried after a
    partial write - the request succeeded, the process died before the commit -
    re-embeds rather than raising, which is the same idempotent-by-re-sweep
    contract the batched passes have.
    """
    with db.pool.connection() as conn:
        conn.cursor().executemany(
            """
            INSERT INTO job_embeddings (url, embedding, model, content_hash,
                                        input_tokens, cost_usd)
            VALUES (%(url)s, %(embedding)s, %(model)s, %(hash)s, %(tokens)s, %(cost)s)
            ON CONFLICT (url) DO UPDATE SET
                embedding = EXCLUDED.embedding, model = EXCLUDED.model,
                content_hash = EXCLUDED.content_hash,
                input_tokens = EXCLUDED.input_tokens, cost_usd = EXCLUDED.cost_usd,
                created_at = now()
            """,
            rows,
        )


async def handle_embed_postings(task_id: int, payload: dict[str, Any]) -> None:
    from openai import AsyncOpenAI

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        # Not an error: a host without a server key simply does no embedding,
        # the same way the batched sweeps no-op without one.
        _set_progress(task_id, 0, 0, "no api key")
        return

    candidates = db.query(_CANDIDATES, {"cap": EMBED_POSTINGS_PER_CYCLE})
    if not candidates:
        _set_progress(task_id, 0, 0, "nothing to embed")
        return

    client = AsyncOpenAI(api_key=key)
    total = len(candidates)
    done = 0
    _set_progress(task_id, 0, total, "embedding postings")
    for start in range(0, total, EMBEDDING_BATCH_SIZE):
        wave = candidates[start : start + EMBEDDING_BATCH_SIZE]
        texts = [r["input_content"][:EMBEDDING_INPUT_CHARS] for r in wave]
        try:
            response = await client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        except Exception as exc:
            # The unembedded urls stay unembedded and the next cycle picks them
            # up; losing the waves already paid for would be the worse failure.
            logger.warning(f"embedding wave failed at offset {start}: {exc}")
            continue
        if len(response.data) != len(wave):
            # The provider returns one vector per input, in order. If that ever
            # stops being true, zipping them would attach every vector to the
            # wrong posting - silently, and permanently.
            logger.error(
                f"embedding wave returned {len(response.data)} vectors "
                f"for {len(wave)} inputs; skipping the wave"
            )
            continue
        # Usage is reported per request, so the per-posting share is the only
        # honest split available; it is recorded so the spend is attributable
        # at all rather than because the split is exact.
        tokens = response.usage.total_tokens if response.usage else 0
        per_posting = tokens // len(wave)
        cost = estimate_cost_usd(EMBEDDING_MODEL, per_posting, 0)
        rows = []
        for row, item in zip(wave, response.data, strict=True):
            if len(item.embedding) != EMBEDDING_DIMENSIONS:
                logger.error(
                    f"embedding for {row['url']} has {len(item.embedding)} dimensions, "
                    f"expected {EMBEDDING_DIMENSIONS}; skipping"
                )
                continue
            text = row["input_content"][:EMBEDDING_INPUT_CHARS]
            rows.append(
                {
                    "url": row["url"],
                    "embedding": str(item.embedding),
                    "model": EMBEDDING_MODEL,
                    "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "tokens": per_posting,
                    "cost": cost,
                }
            )
        if rows:
            _store(rows)
        done += len(wave)
        _set_progress(task_id, done, total, "embedding postings")
    _set_progress(task_id, done, total, "postings embedded")
