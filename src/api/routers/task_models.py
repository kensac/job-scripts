"""Configure task models and show the cost and effect of each choice.

The router chooses from each task's needs and sanctioned models. An owner can
override that choice after seeing its evidence, price, and effect on existing
work.

Model evidence matters. `mail_classify` excludes `gpt-5-nano` after it invented
12 clearances across 55 postings. Each task shape exposes this reason. It also
declares a token profile; dated model rates price a proposed switch.

A sweep skips completed rows regardless of model. Switching models therefore
mixes answers in one catalog without repeating completed work. Model capability
is required; sanctioned status is advice.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin import require_admin
from api.task_config import configured_shape
from core import providers
from core.providers.spec import StructuredOutput
from core.routing import (
    Candidacy,
    ModelChangeEffect,
    NoEligibleModel,
    TaskShape,
    candidates_for,
    resolve,
)
from core.shapes import SHAPES

router = APIRouter(prefix="/admin")


# How many history rows a task view carries. Enough to see the last few
# decisions in context without turning a settings screen into a log viewer;
# the full history is its own endpoint.
RECENT_CHANGES = 10
# A read bound on the history endpoint; every row stays stored.
HISTORY_LIMIT = 200


# How much dearer a model may be than the one running before the change has to
# be acknowledged rather than merely made.
#
# Derived from the largest deliberate step anyone has taken in this codebase:
# gpt-5-nano to gpt-5-mini, which requirements extraction and ongoing mail
# classification both chose on measured evidence, is exactly 5x on input and
# output. Two such steps is 10x, which admits every quality upgrade anyone has
# argued for and stops the ones nobody would argue for - comp on gpt-5.6-sol is
# 60x its current cost, hourly.
#
# Not a refusal. Kanishk owns this system and may overrule it; what he may not
# do is spend 60x by accident. The acknowledgement is a second deliberate act
# and it is recorded on the row.
COST_ACKNOWLEDGEMENT_MULTIPLE = 10


class TaskModelPut(BaseModel):
    # None clears the override and returns the task to what its call site
    # sanctioned. It is not "unset" - a row is still written, because losing
    # the fact that an override existed is the same information loss as
    # overwriting it.
    model: str | None = None
    reason: str | None = Field(default=None, max_length=2000)
    # Required when the new model costs more than COST_ACKNOWLEDGEMENT_MULTIPLE
    # times the current one. The API says what the number is when it refuses,
    # so this is never a guess.
    acknowledge_cost: bool = False


class TaskModelChange(BaseModel):
    """One decision, as the append-only table holds it. A cleared override is
    a row with no model, not a deletion."""

    id: int
    model: str | None
    overrode_sanctioned: bool
    acknowledged_cost: bool
    reason: str | None
    created_at: datetime.datetime
    changed_by_email: str | None


def _history(purpose: str, limit: int) -> list[TaskModelChange]:
    return db.query_as(
        TaskModelChange,
        """
        SELECT o.id, o.model, o.overrode_sanctioned, o.acknowledged_cost, o.reason, o.created_at,
               u.email AS changed_by_email
        FROM task_model_overrides o
        LEFT JOIN users u ON u.id = o.changed_by
        WHERE o.purpose = %(purpose)s
        ORDER BY o.id DESC
        LIMIT %(limit)s
        """,
        {"purpose": purpose, "limit": limit},
    )


def _override(purpose: str) -> str | None:
    """The model the latest decision set, or None when it cleared the override
    or no decision exists."""
    rows = _history(purpose, 1)
    return rows[0].model if rows else None


def _running_model(shape: TaskShape, override: str | None) -> str | None:
    """The override when one is set, else the first sanctioned model."""
    return override or (shape.candidates[0] if shape.candidates else None)


def _known_purpose(purpose: str) -> None:
    if purpose not in SHAPES:
        raise refuse(404, "NOT_FOUND", "unknown task")


# How far back per-model health looks. Derived from the provider's own
# completion window rather than picked: core.batch asks for 24 hours, so a
# window of seven of those covers a week of full cycles and is long enough that
# a single slow batch does not read as a stall.
HEALTH_WINDOW_DAYS = 7


class BatchHealth(BaseModel):
    """One model's batch record in the window, as the query returns it."""

    model: str
    in_flight: int
    last_completed_at: datetime.datetime | None
    oldest_in_flight_at: datetime.datetime | None
    requests: int
    failed_requests: int


class ModelHealth(BaseModel):
    """The same record on the screen, with the window it was measured over."""

    in_flight: int
    last_completed_at: datetime.datetime | None
    oldest_in_flight_at: datetime.datetime | None
    # Numerator and denominator, not a rate. 1 of 3 and 3,000 of 9,000 are
    # different claims and a percentage renders them identically.
    failed_requests: int
    requests: int
    window_days: int


def _model_health() -> dict[str, BatchHealth]:
    """Per model: is it finishing work, and is the work coming back good.

    `failed_requests` over `requests` is the rate that matters - a model can
    complete every batch while failing lines inside them, which is what a
    schema the model keeps breaking looks like.
    """
    rows = db.query_as(
        BatchHealth,
        f"""
        SELECT model,
               COUNT(*) FILTER (WHERE completed_at IS NULL) AS in_flight,
               MAX(completed_at) AS last_completed_at,
               MIN(submitted_at) FILTER (WHERE completed_at IS NULL)
                   AS oldest_in_flight_at,
               COALESCE(SUM(requests), 0) AS requests,
               COALESCE(SUM(failed_count), 0) AS failed_requests
        FROM ai_batches
        WHERE model IS NOT NULL
          AND submitted_at > now() - interval '{HEALTH_WINDOW_DAYS} days'
        GROUP BY model
        """,
    )
    return {r.model: r for r in rows}


def _health_view(row: BatchHealth | None) -> ModelHealth | None:
    """One model's health, or absent when it has run nothing to judge.

    Absent rather than zeroed: a model nobody has used is not a model with a
    perfect record, and a screen that cannot tell those apart will recommend
    the untried one.
    """
    if not row:
        return None
    return ModelHealth(**row.model_dump(exclude={"model"}), window_days=HEALTH_WINDOW_DAYS)


def _money(value: Any) -> str | None:
    """A dollar figure as a plain decimal string, or absent; ModelCandidate
    says why both.

    Formatted with 'f' because str(Decimal) renders an exact zero as "0E-8",
    which is a correct Decimal and an alarming thing to put on a spending
    screen.
    """
    return format(value, "f") if value is not None else None


class ModelEvidence(BaseModel):
    """A measured finding about one model on this task, carried as data so the
    client renders it on the option it is about rather than holding its own
    copy that rots at the next measurement."""

    verdict: str
    finding: str
    sample_size: int
    measured_on: datetime.date


class ModelCandidate(BaseModel):
    """One model this task could run on, eligible or not, carrying WHY.

    Every dollar figure is a decimal string, never a number: these are
    Decimals and a float would round money on the way out. None means unknown
    - an ineligible model has no price and an unbounded sweep has no cycle
    cost - and rendering either as 0 would make an unknown look like a free
    one.
    """

    model: str
    provider: str
    eligible: bool
    rejection: str
    sanctioned: bool
    est_cost_usd: str | None
    est_cycle_cost_usd: str | None
    # Against whatever runs today, so the screen can show the consequence of
    # THIS switch rather than an absolute nobody has a reference for. Absent
    # when either side is unknown.
    est_cycle_cost_delta_usd: str | None
    # True when this model is billed by the hour of day, which makes the
    # figures above the peak ones. A caveat for beside the price, not a state.
    price_varies_by_time: bool
    # Absent when the model has run nothing in the window - which is not the
    # same as healthy, and must not render as zeroes.
    health: ModelHealth | None
    evidence: list[ModelEvidence]


class ResolvedModel(BaseModel):
    """What would run right now, and the grounds for it."""

    model: str
    provider: str
    reason: str
    overridden: bool
    est_cost_usd: str | None
    params: dict[str, str]


class CostBasis(BaseModel):
    """What a cycle cost is computed FROM, so the client can render it as an
    estimate with its basis rather than as a price. per_cycle is the handler's
    own cap; a task with no cap sends 0 and no cycle cost."""

    per_cycle: int
    est_prompt_tokens: int
    max_output_tokens: int
    batched: bool


class TaskModel(BaseModel):
    """One configurable task: what runs it, what else could, and what each
    would cost."""

    purpose: str
    label: str
    notes: str
    on_model_change: ModelChangeEffect
    batched: bool
    structured: StructuredOutput
    sanctioned: list[str]
    override: str | None
    override_is_outside_sanctioned: bool
    # Absent when nothing eligible is left: a configured model that stopped
    # being able to do the work is reported rather than raised, because the
    # screen exists to fix exactly this.
    resolved: ResolvedModel | None
    error: str | None
    candidates: list[ModelCandidate]
    cost_basis: CostBasis
    recent_changes: list[TaskModelChange]


def _candidate_view(
    c: Candidacy,
    shape: TaskShape,
    current_cycle: Decimal | None,
    health: dict[str, BatchHealth],
) -> ModelCandidate:
    return ModelCandidate(
        model=c.model,
        provider=c.provider,
        eligible=c.eligible,
        rejection=c.rejection,
        sanctioned=c.sanctioned,
        est_cost_usd=_money(c.est_cost_usd),
        est_cycle_cost_usd=_money(c.est_cycle_cost_usd),
        est_cycle_cost_delta_usd=_money(
            c.est_cycle_cost_usd - current_cycle
            if c.est_cycle_cost_usd is not None and current_cycle is not None
            else None
        ),
        price_varies_by_time=c.price_varies_by_time,
        health=_health_view(health.get(c.model)),
        evidence=[
            ModelEvidence(
                verdict=e.verdict,
                finding=e.finding,
                sample_size=e.sample_size,
                measured_on=e.measured_on,
            )
            for e in shape.evidence
            if e.model == c.model
        ],
    )


def _view(purpose: str, health: dict[str, BatchHealth]) -> TaskModel:
    shape = configured_shape(SHAPES[purpose])
    override = _override(purpose)
    candidacies = candidates_for(shape)
    current_model = _running_model(shape, override)
    current_cycle = next(
        (c.est_cycle_cost_usd for c in candidacies if c.model == current_model), None
    )
    try:
        chosen = resolve(shape, override=override)
        resolved: ResolvedModel | None = ResolvedModel(
            model=chosen.model,
            provider=chosen.provider,
            reason=chosen.reason,
            overridden=chosen.overridden,
            est_cost_usd=str(chosen.est_cost_usd) if chosen.est_cost_usd else None,
            params={k: str(v) for k, v in chosen.params.items()},
        )
        error = None
    except NoEligibleModel as exc:
        resolved, error = None, str(exc)
    return TaskModel(
        purpose=purpose,
        label=shape.label or purpose,
        notes=shape.notes,
        on_model_change=shape.on_model_change,
        batched=shape.batched,
        structured=shape.structured,
        sanctioned=list(shape.candidates),
        override=override,
        override_is_outside_sanctioned=bool(override) and override not in shape.candidates,
        resolved=resolved,
        error=error,
        # Every declared model, eligible or not, each carrying WHY. A short
        # list with the impossible options silently removed is how someone
        # concludes the missing model is a bug; the rejection reason is the
        # useful half.
        candidates=[_candidate_view(c, shape, current_cycle, health) for c in candidacies],
        cost_basis=CostBasis(
            per_cycle=shape.per_cycle,
            est_prompt_tokens=shape.est_prompt_tokens,
            max_output_tokens=shape.max_output_tokens,
            batched=shape.batched,
        ),
        recent_changes=_history(purpose, RECENT_CHANGES),
    )


def _require_cost_acknowledgement(purpose: str, shape: TaskShape, body: TaskModelPut) -> None:
    """Refuse a large increase unless it was acknowledged, and say how large.

    At the moment of the decision, where the number is exactly known - not
    afterwards, when a sweep declines to run and the person has to work out
    why. A control that fires somewhere other than where the choice was made
    is a control nobody connects to their own action.

    Only increases. Moving to a cheaper model never needs acknowledging.
    """
    if body.acknowledge_cost:
        return
    candidacies = {c.model: c for c in candidates_for(shape)}
    new = candidacies.get(body.model or "")
    current_name = _running_model(shape, _override(purpose))
    current = candidacies.get(current_name or "")
    if new is None or current is None:
        return
    if new.est_cycle_cost_usd is None or not current.est_cycle_cost_usd:
        return
    multiple = new.est_cycle_cost_usd / current.est_cycle_cost_usd
    if multiple <= COST_ACKNOWLEDGEMENT_MULTIPLE:
        return
    raise refuse(
        400,
        "COST_ACKNOWLEDGEMENT_REQUIRED",
        f"{body.model} costs {multiple:.1f}x what {current_name} costs for this "
        f"task - {_money(new.est_cycle_cost_usd)} against "
        f"{_money(current.est_cycle_cost_usd)} per cycle. Re-send with "
        f"acknowledge_cost to proceed.",
        multiple=f"{multiple:.1f}",
        current_model=current_name,
        current_cycle_cost_usd=_money(current.est_cycle_cost_usd),
        new_cycle_cost_usd=_money(new.est_cycle_cost_usd),
        threshold=COST_ACKNOWLEDGEMENT_MULTIPLE,
    )


class TaskModelList(BaseModel):
    tasks: list[TaskModel]


class TaskModelHistory(BaseModel):
    purpose: str
    changes: list[TaskModelChange]


@router.get("/task-models")
def list_task_models(_: AuthedUser = Depends(require_admin)) -> TaskModelList:
    """Every configurable task, with its eligible models and what each costs.

    `est_cost_usd` is per call at the shape's declared token profile.
    `est_cycle_cost_usd` is that times the task's per-cycle cap, read from
    app_config through configured_shape, the same value the sweep reads. A
    task with no cap has no cycle figure: None, never a guessed total.
    """
    # Health spans every model, so one read serves every task.
    health = _model_health()
    return TaskModelList(tasks=[_view(p, health) for p in sorted(SHAPES)])


@router.get("/task-models/{purpose}")
def get_task_model(purpose: str, _: AuthedUser = Depends(require_admin)) -> TaskModel:
    _known_purpose(purpose)
    return _view(purpose, _model_health())


@router.put("/task-models/{purpose}")
def put_task_model(
    purpose: str, body: TaskModelPut, user: AuthedUser = Depends(require_admin)
) -> TaskModel:
    """Set or clear the model for a task.

    Refuses a model the task's declared needs cannot admit - that is capability,
    and accepting it would move the failure to the middle of a paid batch.
    Accepts a model outside the sanctioned set, and records that it was one, so
    the row itself says an override was made rather than leaving it to be
    re-derived later against a sanctioned list that has since moved.
    """
    _known_purpose(purpose)
    shape = configured_shape(SHAPES[purpose])
    if body.model is not None:
        if providers.model(body.model) is None:
            raise refuse(400, "UNKNOWN_MODEL", f"{body.model} is not declared")
        try:
            resolve(shape, override=body.model)
        except NoEligibleModel as exc:
            raise refuse(400, "INELIGIBLE_MODEL", str(exc)) from exc
        _require_cost_acknowledgement(purpose, shape, body)
    db.execute(
        "INSERT INTO task_model_overrides (purpose, model, overrode_sanctioned, reason, "
        "acknowledged_cost, changed_by) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            purpose,
            body.model,
            bool(body.model) and body.model not in shape.candidates,
            body.reason,
            body.acknowledge_cost,
            user.id,
        ),
    )
    return _view(purpose, _model_health())


@router.get("/task-models/{purpose}/history")
def task_model_history(purpose: str, _: AuthedUser = Depends(require_admin)) -> TaskModelHistory:
    """This task's decisions, newest first, up to a read bound.

    The table is append-only and a cleared override is a NULL row rather than a
    deletion, so this is the record and not a reconstruction. A monthly
    review is looking for the switch that explains a regression noticed weeks
    later, which only works if the switch is still here.
    """
    _known_purpose(purpose)
    return TaskModelHistory(purpose=purpose, changes=_history(purpose, HISTORY_LIMIT))
