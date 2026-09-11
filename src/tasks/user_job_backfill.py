"""Resumable split of legacy board scope from person-owned state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from api import db, events
from api.board.person_state import UNTOUCHED, USER_JOB_SPLIT_CHECKPOINT
from tasks.runtime import cancelled, claim_guard

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
CHECKPOINT_KEY = USER_JOB_SPLIT_CHECKPOINT


@dataclass(frozen=True)
class _TaskPayload:
    payload: dict[str, Any]


@dataclass(frozen=True)
class _LegacyRow:
    user_id: int
    job_id: int
    person_touched_at: datetime | None


@dataclass(frozen=True)
class _Key:
    user_id: int
    job_id: int


def _process_batch(task_id: int) -> tuple[dict[str, int], bool] | None:
    owned, owned_params = claim_guard(task_id)
    with db.transaction():
        task = db.query_one_as(
            _TaskPayload,
            f"SELECT payload FROM tasks WHERE id = %(tid)s AND status = 'running'{owned} FOR UPDATE",
            {"tid": task_id, **owned_params},
        )
        if task is None:
            return None
        payload = task.payload
        state = payload.get(CHECKPOINT_KEY) or {}
        last_user_id = int(state.get("user_id") or 0)
        last_job_id = int(state.get("job_id") or 0)
        counts = {
            "already_touched": int(state.get("already_touched") or 0),
            "person_marked": int(state.get("person_marked") or 0),
            "legacy_unknown": int(state.get("legacy_unknown") or 0),
            "working_set_inserted": int(state.get("working_set_inserted") or 0),
        }
        rows = db.query_as(
            _LegacyRow,
            """
            SELECT uj.user_id, uj.job_id, uj.person_touched_at FROM user_jobs uj
            WHERE uj.created_at < %(before)s
              AND (uj.user_id, uj.job_id) > (%(last_user)s, %(last_job)s)
            ORDER BY uj.user_id, uj.job_id
            LIMIT %(limit)s FOR UPDATE
            """,
            {
                "before": payload["legacy_before"],
                "last_user": last_user_id,
                "last_job": last_job_id,
                "limit": BATCH_SIZE,
            },
        )
        if not rows:
            final_progress = {
                "done": sum(counts.values()) - counts["working_set_inserted"],
                "total": int(payload["total"]),
                "label": "legacy job split complete",
                **counts,
            }
            checkpointed = db.execute_count(
                f"UPDATE tasks SET progress = %(progress)s, last_heartbeat = now(), "
                f"progress_at = CASE WHEN progress IS DISTINCT FROM %(progress)s "
                f"                   THEN now() ELSE progress_at END "
                f"WHERE id = %(tid)s AND status = 'running'{owned}",
                {
                    "progress": db.jsonb(final_progress),
                    "tid": task_id,
                    **owned_params,
                },
            )
            if checkpointed != 1:
                raise RuntimeError("lost task claim while finishing user job split")
            return counts, True
        keys = [(row.user_id, row.job_id) for row in rows]
        user_ids = [key[0] for key in keys]
        job_ids = [key[1] for key in keys]
        already = [row for row in rows if row.person_touched_at is not None]
        unknown = db.query_as(
            _Key,
            f"""
            SELECT uj.user_id, uj.job_id FROM user_jobs uj
            WHERE (uj.user_id, uj.job_id) IN (
                SELECT user_id, job_id
                FROM unnest(%s::bigint[], %s::bigint[]) AS keys(user_id, job_id)
            )
              AND uj.person_touched_at IS NULL AND {UNTOUCHED}
            ORDER BY uj.user_id, uj.job_id
            """,
            (user_ids, job_ids),
        )
        unknown_keys = [(row.user_id, row.job_id) for row in unknown]
        unknown_key_set = set(unknown_keys)
        already_key_set = {(row.user_id, row.job_id) for row in already}
        person_keys = [
            key for key in keys if key not in unknown_key_set and key not in already_key_set
        ]
        marked = 0
        if person_keys:
            marked = db.execute_count(
                "UPDATE user_jobs SET person_touched_at = COALESCE(updated_at, created_at) "
                "WHERE (user_id, job_id) IN ("
                "SELECT key_user_id, key_job_id "
                "FROM unnest(%s::bigint[], %s::bigint[]) AS keys(key_user_id, key_job_id)) "
                "AND person_touched_at IS NULL",
                ([key[0] for key in person_keys], [key[1] for key in person_keys]),
            )
        inserted = 0
        if unknown_keys:
            inserted = db.execute_count(
                "INSERT INTO user_job_working_set (user_id, job_id) "
                "SELECT user_id, job_id "
                "FROM unnest(%s::bigint[], %s::bigint[]) AS keys(user_id, job_id) "
                "ON CONFLICT (user_id, job_id) DO NOTHING",
                ([key[0] for key in unknown_keys], [key[1] for key in unknown_keys]),
            )
        counts["already_touched"] += len(already)
        counts["person_marked"] += marked
        counts["legacy_unknown"] += len(unknown_keys)
        counts["working_set_inserted"] += inserted
        last = rows[-1]
        next_state = {
            "user_id": last.user_id,
            "job_id": last.job_id,
            **counts,
        }
        checkpointed = db.execute_count(
            f"""
            UPDATE tasks SET
                payload = jsonb_set(payload, '{{{CHECKPOINT_KEY}}}', %(state)s),
                progress = %(progress)s,
                last_heartbeat = now(),
                progress_at = CASE WHEN progress IS DISTINCT FROM %(progress)s
                                   THEN now() ELSE progress_at END
            WHERE id = %(tid)s AND status = 'running'{owned}
            """,
            {
                "state": db.jsonb(next_state),
                "progress": db.jsonb(
                    {
                        "done": sum(counts.values()) - counts["working_set_inserted"],
                        "total": int(payload["total"]),
                        "label": "splitting legacy job rows",
                        **counts,
                    }
                ),
                "tid": task_id,
                **owned_params,
            },
        )
        if checkpointed != 1:
            raise RuntimeError("lost task claim while checkpointing user job split")
        return counts, len(rows) < BATCH_SIZE


async def handle_backfill_user_job_split(task_id: int, _payload: dict[str, Any]) -> None:
    while not cancelled(task_id):
        result = _process_batch(task_id)
        if result is None:
            return
        counts, finished = result
        events.publish_task(task_id)
        if finished:
            logger.info("backfill_user_job_split complete: %s", counts)
            return
