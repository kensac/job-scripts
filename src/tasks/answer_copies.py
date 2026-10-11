"""Empties the copies answers carried, so the next release can drop them.

An answer's input_content, its usage columns (tokens, cost, duration) and
its instructions are copies: the input rebuilds from the page fetch it names
(core.answer_inputs), the usage is its call's in model_calls, and the
instructions are referenced by instructions_id. Writers store none of them
and ledger_rows reads through the pointers (docs/agents/architecture-
migration.md, phase 8). A column is dropped only once a migration proves it
empty (migrations.md), so this task empties them, one id range per
transaction, and only where nothing is lost:

- input_content, where the answer's fetch rebuilds it byte for byte, or it
  is empty;
- the usage columns, where the answer's call shows the same numbers on it
  (the first answer naming the call) or it is a sibling holding only zeros,
  or it names no call and holds no tokens or cost;
- instructions, always: 0 of 2,826,328 rows held inline text on 2026-10-10
  and nothing reads it (query_instructions.hydrate).

Anything else keeps its copy, is counted, and blocks the drop until it is
looked at. tasks.answer_links runs over each range first, so an answer a
writer left unlinked is linked before it is judged. A run with nothing left
only reads; the worker queues one each cycle until a run clears nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from core import answer_inputs
from tasks import answer_links
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

CHUNK = answer_links.CHUNK
_RANGE = "q.id >= %(lo)s AND q.id < %(hi)s"

_USAGE = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "duration_ms",
    "cost_usd",
)
_TOKENS = _USAGE[:6]
_HELD = " OR ".join(f"q.{c} IS NOT NULL" for c in _USAGE)
_ZERO = " AND ".join(f"COALESCE(q.{c}, 0) = 0" for c in (*_TOKENS, "cost_usd"))
_FIRST = (
    "NOT EXISTS (SELECT 1 FROM ai_queries o "
    "WHERE o.model_call_id = q.model_call_id AND o.id < q.id)"
)
# The call's numbers on the first answer naming it. Token counts a call
# keeps as 0 an older answer kept as NULL, which says the same; duration
# only a live call has.
_SAME_AS_CALL = " AND ".join(
    [f"COALESCE(q.{c}, 0) = COALESCE(m.{c}, 0)" for c in _TOKENS]
    + [
        "q.cost_usd IS NOT DISTINCT FROM m.cost_usd",
        "(q.duration_ms IS NULL OR q.duration_ms = m.duration_ms)",
    ]
)

_CLEAR_INPUT = f"""
    UPDATE ai_queries q SET input_content = NULL
    WHERE {_RANGE} AND q.input_content IS NOT NULL
      AND (q.input_content = '' OR EXISTS (
          SELECT 1 FROM page_fetches f
          WHERE f.id = q.page_fetch_id AND {answer_inputs.sql("q", "f.content")} = q.input_content
      ))
"""

_CLEAR_USAGE = f"""
    UPDATE ai_queries q SET {", ".join(f"{c} = NULL" for c in _USAGE)}
    WHERE {_RANGE} AND ({_HELD})
      AND (
        (q.model_call_id IS NULL AND {_ZERO})
        OR (q.model_call_id IS NOT NULL AND NOT {_FIRST} AND {_ZERO})
        OR (q.model_call_id IS NOT NULL AND {_FIRST} AND EXISTS (
            SELECT 1 FROM model_calls m WHERE m.id = q.model_call_id AND {_SAME_AS_CALL}))
      )
"""

_CLEAR_INSTRUCTIONS = f"""
    UPDATE ai_queries q SET instructions = NULL WHERE {_RANGE} AND q.instructions IS NOT NULL
"""

_STEPS = {
    "inputs_cleared": _CLEAR_INPUT,
    "usage_cleared": _CLEAR_USAGE,
    "instructions_cleared": _CLEAR_INSTRUCTIONS,
}

# What a migration must find empty before it drops the columns.
REMAINING = f"""
    SELECT count(*) FILTER (WHERE input_content IS NOT NULL) AS inputs,
           count(*) FILTER (WHERE {_HELD.replace("q.", "")}) AS usage,
           count(*) FILTER (WHERE instructions IS NOT NULL) AS instructions
    FROM ai_queries
"""


def clear_range(lo: int, hi: int) -> dict[str, int]:
    with db.transaction():
        return {step: db.execute_count(sql, {"lo": lo, "hi": hi}) for step, sql in _STEPS.items()}


def remaining() -> dict[str, int]:
    row = db.query_one(REMAINING)
    assert row is not None
    return {k: int(v) for k, v in row.items()}


async def handle_clear_answer_copies(task_id: int, payload: dict[str, Any]) -> None:
    span = db.query_one("SELECT min(id) AS lo, max(id) AS hi FROM ai_queries")
    if not span or span["lo"] is None:
        set_progress(task_id, 0, 0, "no answers")
        return
    lo, hi = int(span["lo"]), int(span["hi"]) + 1
    done = dict.fromkeys(_STEPS, 0)
    set_progress(task_id, 0, 0, "clearing answer copies")
    for start in range(lo, hi, CHUNK):
        if cancelled(task_id):
            return
        end = min(start + CHUNK, hi)
        await asyncio.to_thread(answer_links.link_range, start, end)
        for step, n in (await asyncio.to_thread(clear_range, start, end)).items():
            done[step] += n
        set_progress(task_id, 0, 0, f"cleared through id {end}", {"cleared": done})
    left = await asyncio.to_thread(remaining)
    # total is what this run cleared: a run that cleared nothing reads 0,
    # which is what stops the worker queueing another.
    total = sum(done.values())
    extra = {"cleared": done, "left": left}
    set_progress(task_id, total, total, f"left {sum(left.values())}", extra)
    logger.info("clear_answer_copies: %s", extra)
