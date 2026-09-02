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


# Postings never embedded, plus postings whose page has been scraped again
# since they were. Same shape as the requirements sweep, for the same reason.
#
# The change check runs over the whole corpus every cycle, so it must not
# detoast it: the first stage takes only the id of each url's current content
# row, which is an index read, and compares it to the id the stored answer came
# from. Only the survivors of that - and only up to the cap - have their text
# fetched. Getting this the other way round would read 110 MB an hour to learn
# that nothing changed.
#
# `stored_hash` rides along so the handler can tell a re-scrape that changed the
# page from one that did not. An identical re-scrape refreshes the id and pays
# for nothing.
_CANDIDATES = f"""
    WITH current_row AS (
        SELECT c.url, q.content_row_id
        FROM (SELECT DISTINCT a.url FROM ai_queries a) c
        {CONTENT_LATERAL.format(url="c.url", columns="id AS content_row_id")}
    ),
    todo AS (
        SELECT cr.url, cr.content_row_id, e.content_hash AS stored_hash
        FROM current_row cr
        LEFT JOIN job_embeddings e ON e.url = cr.url
        WHERE e.url IS NULL
           OR e.content_row_id IS DISTINCT FROM cr.content_row_id
        LIMIT %(cap)s
    )
    SELECT t.url, t.content_row_id, t.stored_hash, q.input_content
    FROM todo t
    {CONTENT_LATERAL.format(url="t.url", columns="input_content")}
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
                                        content_row_id, input_tokens, cost_usd)
            VALUES (%(url)s, %(embedding)s, %(model)s, %(hash)s, %(row_id)s,
                    %(tokens)s, %(cost)s)
            ON CONFLICT (url) DO UPDATE SET
                embedding = EXCLUDED.embedding, model = EXCLUDED.model,
                content_hash = EXCLUDED.content_hash,
                content_row_id = EXCLUDED.content_row_id,
                input_tokens = EXCLUDED.input_tokens, cost_usd = EXCLUDED.cost_usd,
                created_at = now()
            """,
            rows,
        )


def _drop_unchanged_rescrapes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-stamp the pages scraped again that did not change.

    Same reasoning as the requirements sweep: a url reaches this list when its
    current content row is not the one its vector came from, which a re-scrape
    makes true whether or not the text moved. Comparing the stored hash
    separates them, and an unchanged page has its row id refreshed rather than
    being re-embedded - it would produce the same vector at the same cost.
    """
    changed = []
    unchanged: list[tuple[int | None, str]] = []
    for row in rows:
        text = row["input_content"][:EMBEDDING_INPUT_CHARS]
        row["content_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row["stored_hash"] and row["stored_hash"] == row["content_hash"]:
            unchanged.append((row["content_row_id"], row["url"]))
        else:
            changed.append(row)
    if unchanged:
        with db.pool.connection() as conn:
            conn.cursor().executemany(
                "UPDATE job_embeddings SET content_row_id = %s WHERE url = %s", unchanged
            )
        logger.info(f"{len(unchanged)} page(s) re-scraped without changing; not re-embedded")
    return changed


async def handle_embed_postings(task_id: int, payload: dict[str, Any]) -> None:
    from openai import AsyncOpenAI

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        # Not an error: a host without a server key simply does no embedding,
        # the same way the batched sweeps no-op without one.
        _set_progress(task_id, 0, 0, "no api key")
        return

    candidates = _drop_unchanged_rescrapes(db.query(_CANDIDATES, {"cap": EMBED_POSTINGS_PER_CYCLE}))
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
            rows.append(
                {
                    "url": row["url"],
                    "embedding": str(item.embedding),
                    "model": EMBEDDING_MODEL,
                    "hash": row["content_hash"],
                    "row_id": row["content_row_id"],
                    "tokens": per_posting,
                    "cost": cost,
                }
            )
        if rows:
            _store(rows)
        done += len(wave)
        _set_progress(task_id, done, total, "embedding postings")
    _set_progress(task_id, done, total, "postings embedded")
