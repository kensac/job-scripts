"""Empties the usage copies the call ledger replaced, so they can be dropped.

ai_batches' token totals and job_embeddings' cost shares are written by
nothing since #923; model_calls holds every call they described. A column is
dropped only by a migration that proves it empty (migrations.md), and a large
data operation does not go in a migration, so the emptying is this task: one
ordered chunk per statement, each its own transaction, counted before and
after. A run with nothing left only reads. The worker queues it until a run
starts with none left.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# Rows per statement: a reader or the embedding writer waits on one chunk at
# most. job_embeddings held 217,189 non-empty rows on 2026-10-10 (1.8 GB with
# its vectors), so about 22 statements; ai_batches 10,576, so 2.
CHUNK = 10_000

# Each table, its key, and the columns to empty. Rows are locked in key order
# (url in code-point order, as every writer of job_embeddings locks them;
# engineering-standards.md), so a chunk cannot deadlock an upsert.
_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ai_batches": ("id", ("input_tokens", "output_tokens", "cache_write_tokens", "est_cost_usd")),
    "job_embeddings": ('url COLLATE "C"', ("input_tokens", "cost_usd")),
}


def _not_empty(columns: tuple[str, ...]) -> str:
    return " OR ".join(f"{c} IS NOT NULL" for c in columns)


def remaining() -> dict[str, int]:
    counts = {}
    for table, (_, columns) in _TABLES.items():
        row = db.query_one(f"SELECT count(*) AS n FROM {table} WHERE {_not_empty(columns)}")
        counts[table] = int(row["n"]) if row else 0
    return counts


def clear_chunk(table: str) -> int:
    key, columns = _TABLES[table]
    bare_key = key.split()[0]
    return db.execute_count(
        f"UPDATE {table} SET {', '.join(f'{c} = NULL' for c in columns)} "
        f"WHERE {bare_key} IN (SELECT {bare_key} FROM {table} WHERE {_not_empty(columns)} "
        f"ORDER BY {key} LIMIT {CHUNK} FOR UPDATE)"
    )


async def handle_clear_usage_copies(task_id: int, payload: dict[str, Any]) -> None:
    before = await asyncio.to_thread(remaining)
    total = sum(before.values())
    cleared = dict.fromkeys(before, 0)
    set_progress(task_id, 0, total, "clearing usage copies", {"before": before})
    for table in before:
        while not cancelled(task_id):
            n = await asyncio.to_thread(clear_chunk, table)
            if not n:
                break
            cleared[table] += n
            set_progress(task_id, sum(cleared.values()), total, f"clearing {table}")
    after = await asyncio.to_thread(remaining)
    extra = {"before": before, "cleared": cleared, "after": after}
    set_progress(task_id, sum(cleared.values()), total, f"left {sum(after.values())}", extra)
    logger.info("clear_usage_copies: %s", extra)
