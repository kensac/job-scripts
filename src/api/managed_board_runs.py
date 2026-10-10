"""Admission, accounting, and projection for managed-board runs."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from api import budget, db, events, run_configs, task_jobs
from api.board import criteria as board_criteria
from api.board import eligibility
from api.task_admission import ACTIVE_STATUSES, TaskProgress
from api.task_jobs import run_jobs
from core import providers, routing, verdict_reads
from core.batch import BATCH_CHARS_PER_TOKEN
from core.filters import build_custom_decision_instructions, build_custom_input
from core.payload_objects import PayloadStore, PayloadUnavailable
from core.pool import in_transaction
from core.providers import StructuredOutput
from core.routing import NoEligibleModel, TaskShape, resolve
from core.screening import Recipe, TitleGateConfig, screen

# What one verdict is expected to cost in output tokens, for the pre-run
# budget reservation. An ESTIMATE, never a cap: it was passed to the model as
# `max_output_tokens` until 2026-09-12, and every truncated response landed on
# exactly 120 completion tokens with zero variance. 14.8% of one board's run
# died that way, against 0% for the identical prompt and model on the
# user-filter path, which sets no cap at all.
#
# 320 is the measured p95 of a successful custom verdict (p50 91, max 1,050
# over 33,826 decided verdicts on 2026-09-12), so a reservation holds for
# nineteen runs in twenty rather than being three times short.
FILTER_OUTPUT_RESERVATION_TOKENS = 320

# What the batch submission allows, matching `execute_batch`'s own default and
# therefore the user-filter path. Declared so the routing check compares the
# model's ceiling against what is really requested, not against a reservation.
BATCH_OUTPUT_TOKENS = 6000
MANAGED_FILTER_EXECUTION_VERSION = 2
MANAGED_FILTER_TRANSPORT = "batch"
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
ACTIVE_SQL = (
    f"SELECT id FROM tasks WHERE {_KINDS_SQL} AND {_BOARD_SQL} = %s "
    "AND status = ANY(%s) ORDER BY id DESC LIMIT 1"
)


def _batch_shape(model: str, effort: str | None = None) -> TaskShape:
    return TaskShape(
        purpose="managed_board",
        structured=StructuredOutput.JSON_SCHEMA,
        batched=True,
        max_output_tokens=BATCH_OUTPUT_TOKENS,
        est_prompt_tokens=1000,
        candidates=(model,),
        effort=effort,
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
class _Board:
    id: int
    sponsor_user_id: int
    prompt: str
    prompt_hash: str
    requested_model: str
    execution_mode: str
    on_ambiguous: str
    fail_closed: bool
    bypass_sponsorship_filter: bool
    criteria: dict[str, Any]
    title_gate: dict[str, Any] | None
    revision: int
    published: bool
    sources: list[str]


@dataclass(frozen=True)
class _Sponsor:
    id: int
    groups: list[str]


@dataclass(frozen=True)
class _Candidate:
    id: int
    url: str
    company: str
    title: str
    source: str
    sort_at: datetime.datetime
    content_query_id: int | None
    content: str | None


@dataclass(frozen=True)
class _TaskId:
    id: int


@dataclass(frozen=True)
class _ActiveTask:
    id: int


@dataclass(frozen=True)
class _UsagePosition:
    spent: int
    reserved: int


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


def _board(board_id: int, *, lock: bool = False) -> _Board | None:
    suffix = " FOR UPDATE" if lock else ""
    return db.query_one_as(
        _Board,
        "SELECT b.id, b.sponsor_user_id, b.prompt, b.prompt_hash, b.requested_model, b.execution_mode, "
        "b.on_ambiguous, b.fail_closed, b.bypass_sponsorship_filter, b.criteria, b.title_gate, "
        "b.revision, b.published, "
        "COALESCE((SELECT array_agg(s.source ORDER BY s.source) FROM managed_board_sources s "
        "WHERE s.managed_board_id = b.id), '{}') AS sources "
        f"FROM managed_boards b WHERE b.id = %s{suffix}",
        (board_id,),
    )


def _candidates(board: _Board) -> list[_Candidate]:
    params = {
        "sources": board.sources,
        "bypass_sponsorship": board.bypass_sponsorship_filter,
        **board_criteria.params({"criteria": board.criteria}),
    }
    return db.query_as(
        _Candidate,
        f"""
        WITH {eligibility.LATEST_CHECK}
        SELECT j.id, j.url, j.company, j.title, j.source,
               COALESCE(j.date_posted::timestamp AT TIME ZONE 'UTC', j.created_at) AS sort_at,
               content.id AS content_query_id, content.input_content AS content
        FROM jobs j
        LEFT JOIN LATERAL (
          SELECT q.id, q.input_content FROM page_texts q
          WHERE q.url = j.url
          ORDER BY q.id DESC LIMIT 1
        ) content ON true
        WHERE j.source = ANY(%(sources)s)
          AND {eligibility.STRUCTURAL.format(criteria=board_criteria.SQL)}
        ORDER BY j.id
        """,
        params,
    )


def _reservation(board: _Board, candidates: list[_Candidate]) -> int:
    instructions = build_custom_decision_instructions(board.prompt, board.on_ambiguous)
    return sum(
        (
            len(instructions)
            + len(build_custom_input(candidate.company, candidate.title, candidate.content or ""))
        )
        // BATCH_CHARS_PER_TOKEN
        + FILTER_OUTPUT_RESERVATION_TOKENS
        for candidate in candidates
    )


_LATEST_CUSTOM = verdict_reads.latest_per(
    "v.url, v.prompt_hash",
    "v.url, v.prompt_hash, v.status",
    "v.check_type = 'custom' AND v.model = %(model)s "
    "AND v.prompt_hash = ANY(ARRAY(SELECT prompt_hash FROM enabled))",
)


def _reuse_candidates(sponsor_id: int, resolved_model: str) -> list[_Candidate]:
    """Machine-only personal-filter results at one exact verdict identity."""
    params = {"model": resolved_model, **eligibility.settings_params(sponsor_id)}
    return db.query_as(
        _Candidate,
        f"""
        WITH enabled AS ({eligibility.ENABLED_FILTERS}),
        {eligibility.LATEST_CHECK},
        latest_custom AS (
          {_LATEST_CUSTOM}
        )
        SELECT j.id, j.url, j.company, j.title, j.source,
               COALESCE(j.date_posted::timestamp AT TIME ZONE 'UTC', j.created_at) AS sort_at,
               NULL::bigint AS content_query_id, NULL::text AS content
        FROM jobs j
        WHERE {eligibility.SUBSCRIBED}
          AND {eligibility.STRUCTURAL.format(criteria=board_criteria.SQL)}
          AND (SELECT count(*) FROM enabled) > 0
          AND (SELECT count(*) FROM enabled e JOIN latest_custom q
               ON q.url = j.url AND q.prompt_hash = e.prompt_hash
               WHERE q.status = 'passed') = (SELECT count(*) FROM enabled)
        ORDER BY j.id
        """,
        params,
    )


def board_screens(
    title_gate: dict[str, Any] | None, prompt_hash: str, screens: dict[str, Recipe]
) -> list[Recipe]:
    """Every title screen a board's candidates pass: its own gate, then the
    one `title_screens` names for its prompt."""
    recipes: list[Recipe] = (
        [TitleGateConfig.model_validate(title_gate).recipe] if title_gate else []
    )
    if recipe := screens.get(prompt_hash):
        recipes.append(recipe)
    return recipes


def screened(recipes: list[Recipe], title: str | None, source: str | None) -> bool:
    return any(screen(recipe, title=title, source=source).skip for recipe in recipes)


@dataclass(frozen=True)
class BoardQuestion:
    board_id: int
    prompt_hash: str
    criteria: str
    model: str


def verification_questions(
    jobs: list[dict[str, Any]], model: str, effort: str
) -> dict[str, list[BoardQuestion]]:
    """The published boards whose question can ride on each posting's verification.

    A board is asked here only where its own run would buy an answer: the
    posting is from one of its sources, inside its criteria, through its
    title screens, and has no verdict under its
    prompt and model yet. It must also run on the verification model and
    effort, since a verdict is keyed by model and the effort is part of the
    question. Closed and clearance are not checked: verification is what
    answers them, and the board's run applies them to the cached verdict as it
    applies them to its own.
    """
    from core.filters import custom_criteria_instructions
    from core.store import decided_custom_urls

    urls = [job["url"] for job in jobs]
    if not urls:
        return {}
    screens = db.get_config("title_screens")
    questions: dict[str, list[BoardQuestion]] = {}
    for row in db.query(
        "SELECT id FROM managed_boards WHERE published AND execution_mode = 'managed_filter' "
        "ORDER BY id"
    ):
        board = _board(row["id"])
        if board is None or board.requested_model != model:
            continue
        try:
            choice = resolve(_batch_shape(board.requested_model))
        except NoEligibleModel:
            continue
        if choice.params.get("reasoning_effort") != effort:
            continue
        rows = db.query(
            "SELECT j.url, j.title, j.source FROM jobs j "
            "WHERE j.url = ANY(%(urls)s) AND j.source = ANY(%(sources)s) " + board_criteria.SQL,
            {
                "urls": urls,
                "sources": board.sources,
                **board_criteria.params({"criteria": board.criteria}),
            },
        )
        recipes = board_screens(board.title_gate, board.prompt_hash, screens)
        admitted = {r["url"] for r in rows if not screened(recipes, r["title"], r["source"])}
        decided = decided_custom_urls(
            sorted(admitted), board.prompt_hash, model=board.requested_model
        )
        criteria = custom_criteria_instructions(board.prompt, board.on_ambiguous)
        for url in admitted:
            if url in decided:
                continue
            questions.setdefault(url, []).append(
                BoardQuestion(board.id, board.prompt_hash, criteria, board.requested_model)
            )
    return questions


def _allowance(sponsor: _Sponsor) -> tuple[bool, int | None]:
    return budget.owner_budget(sponsor.groups)


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
    active = db.query_one_as(_ActiveTask, ACTIVE_SQL, (board_id, list(ACTIVE_STATUSES)))
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
        owner, cap = _allowance(sponsor)
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
