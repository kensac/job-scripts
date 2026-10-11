"""Stored run summaries, token accounting, and board projection writes."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from api import budget, db, task_jobs
from api.managed_boards.admission import _TaskId
from api.task_admission import TaskProgress
from api.task_jobs import run_jobs

MANAGED_BOARD_RUN_KINDS = task_jobs.MANAGED_BOARD_RUNS.kinds
_KINDS_SQL = task_jobs.MANAGED_BOARD_RUNS.kinds_sql
_BOARD_SQL = "(payload->>'managed_board_id')::bigint"
LATEST_SQL = (
    "SELECT id, status, progress, error, (payload->>'revision')::bigint AS snapshot_revision, "
    "payload->>'requested_model' AS requested_model, "
    "(payload->>'reserved_tokens')::bigint AS reserved_tokens, "
    "created_at, started_at, finished_at "
    f"FROM tasks WHERE {_KINDS_SQL} AND {_BOARD_SQL} = %s ORDER BY id DESC LIMIT 1"
)


class ManagedBoardRun(BaseModel):
    id: int
    status: str
    progress: TaskProgress | None
    error: str | None
    snapshot_revision: int
    requested_model: str
    reserved_tokens: int
    created_at: datetime.datetime
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None


class ManagedBoardCost(BaseModel):
    managed_board_id: int
    calls: int
    total_tokens: int
    cost_usd: float
    week_calls: int
    week_tokens: int
    week_cost_usd: float


@dataclass(frozen=True)
class _CostRow:
    calls: int
    total_tokens: int
    cost_usd: Decimal
    week_calls: int
    week_tokens: int
    week_cost_usd: Decimal


@dataclass(frozen=True)
class _Count:
    n: int


def latest(board_id: int) -> ManagedBoardRun | None:
    return db.query_one_as(ManagedBoardRun, LATEST_SQL, (board_id,))


def cost(board_id: int) -> ManagedBoardCost:
    row = db.query_one_as(
        _CostRow,
        "SELECT COALESCE(sum(requests), 0) AS calls, "
        "COALESCE(sum(total_tokens), 0) AS total_tokens, "
        "COALESCE(sum(cost_usd), 0) AS cost_usd, "
        "COALESCE(sum(requests) FILTER (WHERE created_at >= now() - interval '7 days'), 0) "
        "AS week_calls, "
        "COALESCE(sum(total_tokens) FILTER (WHERE created_at >= now() - interval '7 days'), 0) AS week_tokens, "
        "COALESCE(sum(cost_usd) FILTER (WHERE created_at >= now() - interval '7 days'), 0) AS week_cost_usd "
        "FROM model_calls WHERE managed_board_id = %s",
        (board_id,),
    )
    assert row is not None
    return ManagedBoardCost(
        managed_board_id=board_id,
        calls=row.calls,
        total_tokens=row.total_tokens,
        cost_usd=float(row.cost_usd),
        week_calls=row.week_calls,
        week_tokens=row.week_tokens,
        week_cost_usd=float(row.week_cost_usd),
    )


def record_tokens(
    board_id: int, usage: Mapping[str, int | None], model: str | None, *, batched: bool = False
) -> None:
    if not usage.get("total_tokens"):
        return
    budget.record_managed_board_tokens(board_id, "managed_board", model, usage, batched=batched)


# Who belongs on the board after this run, as (job_id, sort_at). A reuse run
# projects every candidate; a filter run projects what its latest verdict for
# this prompt and model admits.
_REUSE_MEMBERS = """
SELECT * FROM unnest(%(ids)s::bigint[], %(sort)s::timestamptz[]) AS c(job_id, sort_at)
"""
_VERDICT_MEMBERS = """
WITH candidate AS (
  SELECT * FROM unnest(%(ids)s::bigint[], %(sort)s::timestamptz[]) AS c(job_id, sort_at)
), latest AS (
  SELECT DISTINCT ON (j.id) j.id AS job_id, q.status
  FROM candidate c JOIN jobs j ON j.id = c.job_id
  LEFT JOIN ai_queries q ON q.url = j.url AND q.check_type = 'custom'
    AND q.prompt_hash = %(hash)s AND q.model = %(model)s
    AND q.status IN ('passed', 'rejected', 'failed')
  ORDER BY j.id, q.id DESC NULLS LAST
)
SELECT c.job_id, c.sort_at FROM candidate c LEFT JOIN latest l USING (job_id)
WHERE l.status = 'passed' OR (NOT %(fail_closed)s AND l.status IS DISTINCT FROM 'rejected')
"""
# Bring the board's rows to `target`, writing only what differs: delete the
# members that left, update the ones whose sort moved, insert the ones that
# joined. Deleting and reinserting every row cost 78k inserts and
# 78k deletes for a 1,332-row table (pg_stat_user_tables, 2026-10-10), almost
# all of them rewriting a row with itself. One statement, so every part reads
# the rows as they were before it; the three sets are disjoint by job_id.
# projected_at is set on insert only, so it is when the posting joined; the
# run's own time is managed_boards.projection_updated_at.
_WRITE_MEMBERS = """
WITH target AS MATERIALIZED ({target}),
gone AS (
  DELETE FROM managed_board_jobs m
  WHERE m.managed_board_id = %(board)s
    AND NOT EXISTS (SELECT 1 FROM target t WHERE t.job_id = m.job_id)
  RETURNING 1
), moved AS (
  UPDATE managed_board_jobs m
  SET sort_at = t.sort_at
  FROM target t
  WHERE m.managed_board_id = %(board)s AND m.job_id = t.job_id
    AND m.sort_at IS DISTINCT FROM t.sort_at
  RETURNING 1
), joined AS (
  INSERT INTO managed_board_jobs (managed_board_id, job_id, sort_at)
  SELECT %(board)s, t.job_id, t.sort_at
  FROM target t
  WHERE NOT EXISTS (
    SELECT 1 FROM managed_board_jobs m
    WHERE m.managed_board_id = %(board)s AND m.job_id = t.job_id
  )
  ORDER BY t.sort_at DESC, t.job_id DESC
  RETURNING 1
)
SELECT (SELECT count(*) FROM target) AS n
"""


def replace_projection(payload: dict[str, Any], jobs: list[dict[str, Any]] | None = None) -> int:
    jobs = run_jobs(payload) if jobs is None else jobs
    ids = [job["id"] for job in jobs]
    sort_at = [job["sort_at"] for job in jobs]
    with db.transaction():
        board = db.query_one_as(
            _TaskId,
            "SELECT id FROM managed_boards WHERE id = %s AND revision = %s FOR UPDATE",
            (payload["managed_board_id"], payload["revision"]),
        )
        if board is None:
            raise RuntimeError("managed board configuration changed during its run")
        target = (
            _REUSE_MEMBERS
            if payload.get("execution_mode") == "sponsor_filter_reuse"
            else _VERDICT_MEMBERS
        )
        result = db.query_one_as(
            _Count,
            _WRITE_MEMBERS.format(target=target),
            {
                "ids": ids,
                "sort": sort_at,
                "hash": payload["prompt_hash"],
                "model": payload["requested_model"],
                "board": board.id,
                "fail_closed": payload["fail_closed"],
            },
        )
        db.execute(
            "UPDATE managed_boards SET projection_updated_at = now(), "
            "public_revision = CASE WHEN published THEN COALESCE(public_revision, 0) + 1 "
            "ELSE public_revision END WHERE id = %s AND revision = %s",
            (board.id, payload["revision"]),
        )
    return result.n if result else 0
