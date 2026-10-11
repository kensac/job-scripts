"""Move repeated bundle fields from batch_requests to batch_objects.

On 2026-10-11, 1,613,017 request rows named 6,392 objects. Repeated fields
used 378 MB of the 703 MB snapshot_ref data. Readers combine the row and
object through request_snapshots.REF throughout this task.

The payload tracks four phases:

- backfill: link rows only to objects with equal fields.
- gate: wait until all running workers can read through the link.
- clear: link rows missed during backfill, then remove fields only when the
  linked object preserves their values. Count and retain other rows.
- done: a clear pass changed and skipped nothing, and all rows have links
  with no repeated fields.

Each primary-key range is one transaction. SKIP LOCKED leaves busy rows for
another pass. Checkpoints resume interrupted runs; repeated ranges are safe.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, LiteralString

from api import data_level, db
from api.ai.request_snapshots import OBJECT
from tasks.runtime import cancelled, checkpoint

logger = logging.getLogger(__name__)

KIND = "consolidate_batch_objects"

# Rows per transaction. A 5,000 row range read 2,507 buffers in 102 ms on
# production (2026-10-11), and locks at most that many rows.
CHUNK = 5000

_FIELDS: LiteralString = "'{bucket,key,sha256,size,version}'::text[]"
_RANGE: LiteralString = (
    "(q.task_id, q.custom_id) > (%(t)s, %(c)s) AND (q.task_id, q.custom_id) <= (%(lt)s, %(lc)s)"
)

_LAST: LiteralString = """
SELECT task_id, custom_id FROM (
    SELECT task_id, custom_id FROM batch_requests
    WHERE (task_id, custom_id) > (%(t)s, %(c)s) ORDER BY task_id, custom_id LIMIT %(n)s
) s ORDER BY task_id DESC, custom_id DESC LIMIT 1
"""

_OBJECTS: LiteralString = f"""
INSERT INTO batch_objects (bucket, key, sha256, size, version)
SELECT DISTINCT q.snapshot_ref->>'bucket', q.snapshot_ref->>'key', q.snapshot_ref->>'sha256',
       (q.snapshot_ref->>'size')::bigint, (q.snapshot_ref->>'version')::int
FROM batch_requests q
WHERE {_RANGE} AND q.object_id IS NULL AND q.snapshot_ref ?& {_FIELDS}
ORDER BY 1, 2
ON CONFLICT (bucket, key) DO NOTHING
"""

_FILL: LiteralString = f"""
UPDATE batch_requests q SET object_id = o.id
FROM batch_objects o
WHERE (q.task_id, q.custom_id) IN (
        SELECT task_id, custom_id FROM batch_requests q
        WHERE {_RANGE} AND q.object_id IS NULL FOR UPDATE SKIP LOCKED)
  AND o.bucket = q.snapshot_ref->>'bucket' AND o.key = q.snapshot_ref->>'key'
  AND q.snapshot_ref @> {OBJECT}
"""

# @> is the equality proof: the row holds exactly the object row's values,
# types included, so removing them loses nothing REF does not give back.
_CLEAR: LiteralString = f"""
UPDATE batch_requests q SET snapshot_ref = q.snapshot_ref - {_FIELDS}
FROM batch_objects o
WHERE o.id = q.object_id
  AND (q.task_id, q.custom_id) IN (
        SELECT task_id, custom_id FROM batch_requests q
        WHERE {_RANGE} AND q.snapshot_ref ?| {_FIELDS} FOR UPDATE SKIP LOCKED)
  AND q.snapshot_ref @> {OBJECT}
"""

_COUNT: LiteralString = f"""
SELECT count(*) AS rows,
       count(*) FILTER (WHERE q.object_id IS NULL) AS unpointed,
       count(*) FILTER (WHERE q.snapshot_ref ?| {_FIELDS}) AS holding
FROM batch_requests q WHERE {_RANGE}
"""

_COUNTS = ("rows", "filled", "cleared", "unpointed", "holding")


def _pass(phase: str) -> dict[str, Any]:
    return {"phase": phase, "t": 0, "c": "", **dict.fromkeys(_COUNTS, 0)}


START = _pass("backfill")


def chunk(state: dict[str, Any], *, clear: bool, limit: int = CHUNK) -> dict[str, Any] | None:
    """One range of rows: point them, clear them if `clear`, count what is
    left. None at the end of the table."""
    params: dict[str, Any] = {"t": state["t"], "c": state["c"], "n": limit}
    last = db.query_one(_LAST, params)
    if last is None:
        return None
    params |= {"lt": last["task_id"], "lc": last["custom_id"]}
    with db.transaction():
        db.execute("SET LOCAL lock_timeout = '2s'")
        db.execute(_OBJECTS, params)
        filled = db.execute_count(_FILL, params)
        cleared = db.execute_count(_CLEAR, params) if clear else 0
        counts = db.query_one(_COUNT, params)
    assert counts is not None
    return {
        **state,
        "t": last["task_id"],
        "c": last["custom_id"],
        "rows": state["rows"] + counts["rows"],
        "filled": state["filled"] + filled,
        "cleared": state["cleared"] + cleared,
        "unpointed": state["unpointed"] + counts["unpointed"],
        "holding": state["holding"] + counts["holding"],
    }


def advance(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """One step. True when this run should stop; the next run resumes from
    the state returned."""
    phase = state["phase"]
    if phase == "done":
        return state, True
    if phase in ("gate", "clear"):
        behind = data_level.behind(data_level.LEVEL)
        if behind:
            # A host rolled back mid-pass stops the clearing too.
            return {**state, "phase": "gate", "behind": behind}, True
        if phase == "gate":
            return _pass("clear"), False
    following = chunk(state, clear=phase == "clear")
    if following is not None:
        return following, False
    if phase == "backfill":
        return _pass("gate"), False
    if not (state["cleared"] or state["unpointed"] or state["holding"]):
        return {**state, "phase": "done"}, True
    # Another pass, from the next run: what one pass cleared, skipped or
    # found unequal is checked again from the start.
    logger.info("%s: pass ended %s", KIND, state)
    return _pass("clear"), True


def _resume(task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("state"):
        return payload["state"]
    row = db.query_one(
        "SELECT payload->'state' AS state FROM tasks "
        "WHERE kind = %s AND id <> %s AND payload ? 'state' ORDER BY id DESC LIMIT 1",
        (KIND, task_id),
    )
    return row["state"] if row and row["state"] else START


def _estimate() -> int:
    row = db.query_one(
        "SELECT GREATEST(reltuples, 0)::bigint AS n FROM pg_class WHERE oid = 'batch_requests'::regclass"
    )
    return int(row["n"]) if row else 0


async def handle_consolidate_batch_objects(task_id: int, payload: dict[str, Any]) -> None:
    state = await asyncio.to_thread(_resume, task_id, payload)
    total = await asyncio.to_thread(_estimate)
    while not cancelled(task_id):
        state, stop = await asyncio.to_thread(advance, state)
        label = f"{state['phase']}: {state['rows']} rows"
        if not checkpoint(task_id, state["rows"], total, label, state, payload={"state": state}):
            return
        if stop:
            break
    logger.info("%s: %s", KIND, state)
