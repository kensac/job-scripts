"""Embeds each posting once, so the corpus can be asked what resembles what."""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from api import db
from api.board import visibility
from core.batch import BatchResult, BatchSpec
from core.embeddings import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_INPUT_CHARS,
    EMBEDDING_MODEL,
)
from core.store import CONTENT_LATERAL
from tasks.derive import Derivation, Row

logger = logging.getLogger(__name__)


# The original synchronous exception was measured at $0.47 for the corpus
# versus $0.23 batched, with calls returning in about a second. It avoided
# widening the then response-only batch collector for twenty-four cents.
# The shared collector now preserves endpoint-specific snapshots and results;
# scheduled embeddings use it so the worker can release its slot while waiting.


# Postings never embedded, plus postings whose page has been scraped again
# since they were. Same shape as the requirements sweep, for the same reason.
#
# The scope is the personal similarity reader's membership, including
# ownership exceptions. A url with no job row is excluded because no
# similarity route can address it.
#
# The first stage projects only the current content ID and compares it with
# stored provenance before returning text for capped survivors. This limits
# returned text, not database detoasting: CONTENT_LATERAL's length predicate
# still inspects historical text. Do not claim an index-only scan here.
#
# `stored_hash` rides along so the handler can tell a re-scrape that changed the
# page from one that did not. An identical re-scrape refreshes the id and pays
# for nothing.
def _candidate_sql(scope: str) -> str:
    return f"""
    WITH current_row AS (
        SELECT c.url, q.content_row_id
        FROM ({scope}) c
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


_CANDIDATES = _candidate_sql(visibility.across_users("j.url"))


def _store(rows: list[dict[str, Any]]) -> int:
    return db.execute_count(
        """
        INSERT INTO job_embeddings (url, embedding, model, content_hash, content_row_id)
        SELECT r.url, r.embedding::vector, r.model, r.hash, r.row_id
        FROM jsonb_to_recordset(%s) AS r(
            url text, embedding text, model text, hash text, row_id bigint
        )
        ON CONFLICT (url) DO UPDATE SET
            embedding = EXCLUDED.embedding, model = EXCLUDED.model,
            content_hash = EXCLUDED.content_hash,
            content_row_id = EXCLUDED.content_row_id,
            created_at = now()
        WHERE job_embeddings.content_row_id IS NULL
           OR job_embeddings.content_row_id <= EXCLUDED.content_row_id
        """,
        (db.jsonb(rows),),
    )


def _select(cap: int, payload: dict[str, Any]) -> list[Row]:
    if not os.environ.get("OPENAI_API_KEY"):
        logger.info("no api key; nothing embedded")
        return []
    return db.query(_CANDIDATES, {"cap": cap})


def _requests(rows: list[Row]) -> list[BatchSpec]:
    specs = []
    for start in range(0, len(rows), EMBEDDING_BATCH_SIZE):
        wave = rows[start : start + EMBEDDING_BATCH_SIZE]
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
    return specs


def _store_result(result: BatchResult, context: dict[str, Any], _: None) -> str:
    """One request carries up to EMBEDDING_BATCH_SIZE postings, so the page
    currency check is per posting here, in one statement for the request."""
    request = result.request
    if request is None or request.endpoint != "/v1/embeddings":
        return "unknown_request"
    if not result.model:
        return "unknown_model"
    vectors = result.embedding_vectors
    originals = context["rows"]
    if result.error or vectors is None or len(vectors) != len(originals):
        return "failed"
    current = {
        row["url"]: row["id"]
        for row in db.query(
            "SELECT page.url,q.id FROM unnest(%s::text[]) AS page(url) "
            + CONTENT_LATERAL.format(url="page.url", columns="id"),
            ([row["url"] for row in originals],),
        )
    }
    # The packed request's usage is the call ledger's (model_calls), booked
    # with its receipt. A posting's equal share of it was a guess nothing
    # read, and it is no longer stored.
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
            }
        )
    written = _store(rows) if rows else 0
    return "written" if written else "discarded"


# One pass in flight at a time (the shared sweep's guard), so a backfill
# drains one provider turnaround per cycle, not hourly; the hourly cadence
# holds only once the backlog is gone. The model is the recipe: the table
# stores it, and nothing re-embeds when it changes.
EMBEDDINGS = Derivation(
    kind="embed_postings_batch",
    purpose="embedding",
    noun="embedding",
    table="job_embeddings",
    per_cycle_key="embed_postings_per_cycle",
    select=_select,
    requests=_requests,
    store=_store_result,
    input_chars=EMBEDDING_INPUT_CHARS,
    recipe=None,
    model=EMBEDDING_MODEL,
    context_keys=("rows",),
    skip_unchanged=True,
)
