"""Candidate selection and verification questions for managed boards."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from api import db
from api.board import criteria as board_criteria
from api.board import eligibility
from core import verdict_reads
from core.batch import BATCH_CHARS_PER_TOKEN
from core.filters import build_custom_decision_instructions, build_custom_input
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
class _Candidate:
    id: int
    url: str
    company: str
    title: str
    source: str
    sort_at: datetime.datetime
    content_query_id: int | None
    # The page text's length, which is all the reservation reads. Returning
    # the text itself moved 215M characters for board 6's 35,310 candidates
    # (104 s against 15 s on production, 2026-10-18) and held a worker's main
    # thread past its heartbeat.
    content_chars: int | None


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
        SELECT j.id, j.url, j.company, j.title, j.source,
               COALESCE(j.date_posted::timestamp AT TIME ZONE 'UTC', j.created_at) AS sort_at,
               content.id AS content_query_id, content.chars AS content_chars
        FROM jobs j
        LEFT JOIN LATERAL (
          SELECT q.id, q.chars FROM page_texts q
          WHERE q.url = j.url
          ORDER BY q.id DESC LIMIT 1
        ) content ON true
        WHERE j.source = ANY(%(sources)s)
          AND {eligibility.STRUCTURAL_PER_POSTING.format(criteria=board_criteria.SQL)}
        ORDER BY j.id
        """,
        params,
    )


def _reservation(board: _Board, candidates: list[_Candidate]) -> int:
    instructions = build_custom_decision_instructions(board.prompt, board.on_ambiguous)
    return sum(
        (
            len(instructions)
            + len(build_custom_input(candidate.company, candidate.title, ""))
            + (candidate.content_chars or 0)
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
               NULL::bigint AS content_query_id, NULL::int AS content_chars
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
