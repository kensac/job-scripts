"""Embeds each posting once, so the corpus can be asked what resembles what."""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from api import batch_results, db
from api.task_admission import ACTIVE_STATUSES
from api.tasks import rescrape
from api.tasks.runtime import (
    batch_event_hook,
    consume_result,
    enqueue,
    has_batch_work,
    set_progress,
    submit_or_collect,
)
from core.batch import BatchSpec
from core.embeddings import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_INPUT_CHARS,
    EMBEDDING_MODEL,
)
from core.pricing import estimate_cost_usd
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL

logger = logging.getLogger("jobtracker_worker")


# The original synchronous exception was measured at $0.47 for the corpus
# versus $0.23 batched, with calls returning in about a second. It avoided
# widening the then response-only batch collector for twenty-four cents.
# The shared collector now preserves endpoint-specific snapshots and results;
# scheduled embeddings use it so the worker can release its slot while waiting.
# Keep the measured 2,000-posting cycle bound: at 100 inputs per provider
# request this is 20 requests and the original corpus drained in 11 cycles.
EMBED_POSTINGS_PER_CYCLE = int(os.environ.get("JOBTRACKER_EMBED_POSTINGS_PER_CYCLE", "2000"))


# Postings never embedded, plus postings whose page has been scraped again
# since they were. Same shape as the requirements sweep, for the same reason.
#
# Scoped to postings a person can reach. This sweep started from every url
# with an ai_queries row, so a job kept being re-read for as long as it
# existed, whether or not anyone had enabled its board.
#
# A url with NO job row stays in, and the LEFT JOIN is what keeps it. This
# sweep is url-keyed on purpose: a fifth of the corpus is postings whose job
# row is gone and whose page can never be scraped again, and joining `jobs`
# to reach the gate would have dropped every one of them silently. An orphan
# has no source to judge, so the gate has nothing to say about it.
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
        FROM (
            SELECT DISTINCT a.url FROM ai_queries a
            LEFT JOIN jobs j ON j.url = a.url
            WHERE j.url IS NULL OR {AI_ELIGIBLE_JOB.format(job="j")}
        ) c
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


def _store(rows: list[dict[str, Any]]) -> int:
    return db.execute_count(
        """
        INSERT INTO job_embeddings (url, embedding, model, content_hash,
                                    content_row_id, input_tokens, cost_usd)
        SELECT r.url, r.embedding::vector, r.model, r.hash, r.row_id, r.tokens, r.cost
        FROM jsonb_to_recordset(%s) AS r(
            url text, embedding text, model text, hash text, row_id bigint,
            tokens bigint, cost numeric
        )
        ON CONFLICT (url) DO UPDATE SET
            embedding = EXCLUDED.embedding, model = EXCLUDED.model,
            content_hash = EXCLUDED.content_hash,
            content_row_id = EXCLUDED.content_row_id,
            input_tokens = EXCLUDED.input_tokens, cost_usd = EXCLUDED.cost_usd,
            created_at = now()
        WHERE job_embeddings.content_row_id IS NULL
           OR job_embeddings.content_row_id <= EXCLUDED.content_row_id
        """,
        (db.jsonb(rows),),
    )


async def handle_embed_postings(task_id: int, payload: dict[str, Any]) -> None:
    # Older images know only this kind. The new kind keeps them from claiming
    # paid embedding snapshots with the former synchronous implementation.
    child = enqueue(
        "embed_postings_batch", payload, dedupe_key=f"embed-batch:{payload.get('cycle', task_id)}"
    )
    set_progress(task_id, 0, 0, f"queued embedding batch task {child}")


async def handle_embed_postings_batch(task_id: int, payload: dict[str, Any]) -> None:
    specs = []
    if not has_batch_work(task_id):
        if not os.environ.get("OPENAI_API_KEY"):
            set_progress(task_id, 0, 0, "no api key")
            return
        earlier = db.query_one(
            "SELECT id FROM tasks WHERE kind='embed_postings_batch' AND id<%s "
            "AND status=ANY(%s) ORDER BY id LIMIT 1",
            (task_id, list(ACTIVE_STATUSES)),
        )
        if earlier:
            # One embedding batch in flight at a time. A cycle therefore waits
            # for the previous batch to come back before it submits, so a
            # backfill drains one provider turnaround per cycle, not hourly;
            # the hourly cadence holds only once the backlog is gone.
            set_progress(task_id, 0, 0, f"embedding task {earlier['id']} is still in flight")
            return
        candidates = rescrape.drop_unchanged(
            db.query(_CANDIDATES, {"cap": EMBED_POSTINGS_PER_CYCLE}),
            table="job_embeddings",
            limit=EMBEDDING_INPUT_CHARS,
        )
        for start in range(0, len(candidates), EMBEDDING_BATCH_SIZE):
            wave = candidates[start : start + EMBEDDING_BATCH_SIZE]
            specs.append(
                BatchSpec(
                    f"embeddings:{start // EMBEDDING_BATCH_SIZE}",
                    inputs=[row["input_content"][:EMBEDDING_INPUT_CHARS] for row in wave],
                    endpoint="/v1/embeddings",
                    context={
                        "dimensions": EMBEDDING_DIMENSIONS,
                        "rows": [
                            {
                                "url": row["url"],
                                "content_row_id": row["content_row_id"],
                                "content_hash": row["content_hash"],
                            }
                            for row in wave
                        ],
                    },
                )
            )
        if not specs:
            set_progress(task_id, 0, 0, "nothing to embed")
            return
        set_progress(task_id, 0, len(specs), "embedding requests submitted")

    hook = batch_event_hook(task_id, "embedding", EMBEDDING_MODEL)
    results = await submit_or_collect(task_id, specs, EMBEDDING_MODEL, "", 0, hook)
    for result in results:
        with consume_result(task_id, result) as receipt:
            if not receipt.pending:
                continue
            request = result.request
            context = request.context if request else None
            if request is None or not context or request.endpoint != "/v1/embeddings":
                receipt.outcome = "unknown_request"
                continue
            if not result.model:
                receipt.outcome = "unknown_model"
                continue
            vectors = result.embedding_vectors
            originals = context["rows"]
            if result.error or vectors is None or len(vectors) != len(originals):
                receipt.outcome = "failed"
                continue
            current = {
                row["url"]: row["id"]
                for row in db.query(
                    "SELECT page.url,q.id FROM unnest(%s::text[]) AS page(url) "
                    + CONTENT_LATERAL.format(url="page.url", columns="id"),
                    ([row["url"] for row in originals],),
                )
            }
            # Provider usage is exact for the packed request, recorded by the
            # fleet hook. Per-posting fields remain approximate equal shares,
            # as on the live path; the provider does not report individual usage.
            tokens = (result.usage or {}).get("input_tokens")
            per_posting = tokens // len(originals) if tokens is not None else None
            cost = (
                estimate_cost_usd(result.model, per_posting, 0, batched=True)
                if per_posting is not None
                else None
            )
            rows = []
            for original, vector in zip(originals, vectors, strict=True):
                if len(vector) != context["dimensions"] or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in vector
                ):
                    continue
                if current.get(original["url"]) != original["content_row_id"]:
                    continue
                rows.append(
                    {
                        "url": original["url"],
                        "embedding": str(vector),
                        "model": result.model,
                        "hash": original["content_hash"],
                        "row_id": original["content_row_id"],
                        "tokens": per_posting,
                        "cost": cost,
                    }
                )
            written = _store(rows) if rows else 0
            receipt.outcome = "written" if written else "discarded"
    done, total = batch_results.progress_counts(task_id)
    set_progress(task_id, done, total, "embedding requests applied")
