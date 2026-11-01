"""Budget admission and atomic task creation for managed board runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from api import budget, db, events, run_configs, task_jobs
from api.managed_boards.selection import (
    MANAGED_FILTER_EXECUTION_VERSION,
    MANAGED_FILTER_TRANSPORT,
    _batch_shape,
    _Board,
    _board,
    _candidates,
    _reservation,
    _reuse_candidates,
    board_screens,
    screened,
)
from api.task_admission import ACTIVE_STATUSES
from core import providers, routing
from core.payloads.objects import PayloadStore, PayloadUnavailable
from core.pool import in_transaction
from core.routing import NoEligibleModel, resolve
from core.screening import Recipe

MANAGED_BOARD_RUN_KINDS = task_jobs.MANAGED_BOARD_RUNS.kinds
_KINDS_SQL = task_jobs.MANAGED_BOARD_RUNS.kinds_sql
_BOARD_SQL = "(payload->>'managed_board_id')::bigint"
ACTIVE_SQL = (
    f"SELECT id FROM tasks WHERE {_KINDS_SQL} AND {_BOARD_SQL} = %s "
    "AND status = ANY(%s) ORDER BY id DESC LIMIT 1"
)
# Advisory lock class for a sponsor's allowance, keyed (class, user id) like
# api.board.visibility's 7001.
_SPONSOR_LOCK = 7002


class ManagedBoardRunQueued(BaseModel):
    task_id: int
    reserved_tokens: int
    candidate_count: int


class RunRefusal(Exception):
    def __init__(self, code: str, message: str, *, task_id: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.task_id = task_id


@dataclass(frozen=True)
class _Sponsor:
    id: int
    groups: list[str]


@dataclass(frozen=True)
class _TaskId:
    id: int


@dataclass(frozen=True)
class _UsagePosition:
    spent: int
    reserved: int


def _usage_position(sponsor_id: int) -> _UsagePosition:
    row = db.query_one_as(
        _UsagePosition,
        """
        SELECT
          COALESCE((SELECT SUM(a.total_tokens) FROM model_calls a
                    LEFT JOIN managed_boards b ON b.id = a.managed_board_id
                    WHERE a.key_source = 'owner'
                      AND a.created_at >= now() - interval '7 days'
                      AND (a.user_id = %(sponsor)s OR b.sponsor_user_id = %(sponsor)s)), 0)::bigint AS spent,
          COALESCE((SELECT SUM((t.payload->>'reserved_tokens')::bigint)
                    FROM tasks t JOIN managed_boards b
                      ON b.id = (t.payload->>'managed_board_id')::bigint
                    WHERE t.kind = ANY(%(kinds)s) AND t.status = ANY(%(active)s)
                      AND b.sponsor_user_id = %(sponsor)s), 0)::bigint AS reserved
        """,
        {
            "sponsor": sponsor_id,
            "active": list(ACTIVE_STATUSES),
            "kinds": list(MANAGED_BOARD_RUN_KINDS),
        },
    )
    assert row is not None
    return row


@dataclass(frozen=True)
class _Plan:
    board: _Board
    payload: dict[str, Any]
    jobs: list[dict[str, Any]]
    reserved: int
    cap: int | None


def _refuse_active(board_id: int) -> None:
    active = db.query_one_as(_TaskId, ACTIVE_SQL, (board_id, list(ACTIVE_STATUSES)))
    if active:
        raise RunRefusal("IN_PROGRESS", "this board already has an active run", task_id=active.id)


def _refuse_over_budget(sponsor_id: int, cap: int | None, reserved: int) -> None:
    position = _usage_position(sponsor_id)
    if cap is not None and position.spent + position.reserved + reserved > cap:
        raise RunRefusal(budget.BUDGET_EXCEEDED, "sponsor weekly allowance cannot cover this run")


def _plan(board_id: int) -> _Plan:
    board = _board(board_id)
    if board is None:
        raise RunRefusal("NOT_FOUND", "unknown managed board")
    _refuse_active(board_id)
    sponsor = db.query_one_as(
        _Sponsor, "SELECT id, groups FROM users WHERE id = %s", (board.sponsor_user_id,)
    )
    if sponsor is None:
        raise RunRefusal("NO_SPONSOR", "managed board sponsor no longer exists")
    resolved_model = board.requested_model
    reasoning_effort: object | None = None
    cap: int | None = None
    reserved = 0
    recipes: list[Recipe] = []
    if board.execution_mode == "sponsor_filter_reuse":
        try:
            _entitlement, config = budget.load_config(sponsor.id, ignore_budget=True)
        except budget.AIAccessError as exc:
            raise RunRefusal(
                exc.reason, "the sponsor has no resolvable personal filter model"
            ) from exc
        resolved_model = config.model
        candidates = _reuse_candidates(sponsor.id, resolved_model)
    else:
        owner, cap = budget.owner_budget(sponsor.groups)
        provider = providers.provider_of(board.requested_model)
        if not owner or provider is None or not routing.server_key(provider):
            raise RunRefusal("NO_SERVER_KEY", "the sponsor has no server-key allowance")
        if provider != "openai":
            raise RunRefusal(
                "BATCH_UNSUPPORTED",
                "requested model cannot execute through the managed batch collector",
            )
        if board.requested_model not in budget.owner_allowed_models(sponsor.groups):
            raise RunRefusal("MODEL_NOT_ALLOWED", "requested model is not allowed for the sponsor")
        try:
            choice = resolve(_batch_shape(board.requested_model))
        except NoEligibleModel as exc:
            raise RunRefusal(
                "BATCH_UNSUPPORTED",
                "requested model cannot execute managed-board batches",
            ) from exc
        reasoning_effort = choice.params.get("reasoning_effort")
        # A screened posting never enters the run: it gets no verdict, and the
        # projection shows only what the run holds. Kept in the run's list, it
        # was carried, partitioned and recorded as a skip on every run.
        recipes = board_screens(board.title_gate, board.prompt_hash, db.get_config("title_screens"))
        candidates = [c for c in _candidates(board) if not screened(recipes, c.title, c.source)]
        reserved = _reservation(board, candidates)
        _refuse_over_budget(sponsor.id, cap, reserved)
    jobs = [
        {
            "id": candidate.id,
            "url": candidate.url,
            "company": candidate.company,
            "title": candidate.title,
            "source": candidate.source,
            "sort_at": candidate.sort_at.isoformat(),
            "content_query_id": candidate.content_query_id,
        }
        for candidate in candidates
    ]
    settings = {
        "prompt": board.prompt,
        "prompt_hash": board.prompt_hash,
        "on_ambiguous": board.on_ambiguous,
        "fail_closed": board.fail_closed,
        "bypass_sponsorship_filter": board.bypass_sponsorship_filter,
        "sources": board.sources,
        "criteria": board.criteria,
    }
    payload = {
        "managed_board_id": board.id,
        "sponsor_user_id": board.sponsor_user_id,
        "revision": board.revision,
        "config_id": run_configs.intern(run_configs.BOARD, settings),
        "requested_model": resolved_model,
        "execution_mode": board.execution_mode,
        "title_screens": recipes,
        "published": board.published,
        "reserved_tokens": reserved,
        "candidate_count": len(jobs),
    }
    if board.execution_mode == "managed_filter":
        payload.update(
            {
                "execution_version": MANAGED_FILTER_EXECUTION_VERSION,
                "inference_transport": MANAGED_FILTER_TRANSPORT,
                "reasoning_effort": reasoning_effort,
            }
        )
    return _Plan(board, payload, jobs, reserved, cap)


def admit(board_id: int, *, dedupe_key: str | None = None) -> ManagedBoardRunQueued:
    """Plan, store the candidates, then insert under the board lock.

    The candidate list is 8 to 9 MB of JSON per run (2026-10-03), so it is a
    verified object and the task holds only its reference. The upload cannot
    run inside a transaction, so everything the plan read that a concurrent
    writer can change (the board, an active run, the sponsor's reservations)
    is checked again under the lock before the insert.

    The allowance is the sponsor's, shared by every board they sponsor, so the
    recheck holds the sponsor's lock as well as the board's: under the board
    lock alone, two of one sponsor's boards could each pass the check without
    seeing the other's reservation. Sponsor first, then board, in every
    admission.
    """
    if in_transaction():
        raise RuntimeError("Managed board admission uploads outside a database transaction")
    # Every worker schedules every cycle; one already admitted needs no plan.
    if dedupe_key and db.query_one("SELECT 1 FROM tasks WHERE dedupe_key = %s", (dedupe_key,)):
        raise RunRefusal("ALREADY_SCHEDULED", "this board was already scheduled this cycle")
    plan = _plan(board_id)
    try:
        ref = PayloadStore.from_env().put_verified(plan.jobs)
    except PayloadUnavailable as exc:
        raise RunRefusal("STORAGE_UNAVAILABLE", "run candidates could not be stored") from exc
    payload = {**plan.payload, **task_jobs.reference(task_jobs.MANAGED_BOARD_RUNS, plan.jobs, ref)}
    with db.transaction():
        db.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (_SPONSOR_LOCK, plan.board.sponsor_user_id)
        )
        if _board(board_id, lock=True) != plan.board:
            raise RunRefusal("BOARD_CHANGED", "the board changed while its run was planned")
        _refuse_active(board_id)
        _refuse_over_budget(plan.board.sponsor_user_id, plan.cap, plan.reserved)
        task_kind = (
            "run_managed_board_batch"
            if plan.board.execution_mode == "managed_filter"
            else "run_managed_board"
        )
        row = db.query_one_as(
            _TaskId,
            "INSERT INTO tasks (kind, payload, dedupe_key) VALUES (%s, %s, %s) "
            "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            (task_kind, db.jsonb(payload), dedupe_key),
        )
        if row is None:
            raise RunRefusal("ALREADY_SCHEDULED", "this board was already scheduled this cycle")
        task_id = row.id
    events.publish_task(task_id)
    return ManagedBoardRunQueued(
        task_id=task_id, reserved_tokens=plan.reserved, candidate_count=len(plan.jobs)
    )
