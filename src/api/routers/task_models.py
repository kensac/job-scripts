"""Configuring which model runs which task, and saying what that costs.

The router already decides this from a task's declared needs and the models
its call site sanctioned. This lets the owner of the system overrule that
choice from a screen - and, more importantly, refuses to let him do it blind.

Three things this deliberately puts in front of a person before they commit:

WHAT THE CALL SITE KNEW. Model choice here is an evidence judgment, not a
preference. mail_classify excludes gpt-5-nano because it was measured
fabricating - 12 invented clearances across 55 postings. That reasoning lived
in a code comment, which is invisible to exactly the person about to overrule
it, so it is now a field on the shape and it is on this screen.

WHAT IT COSTS. Every shape declares its own token profile, and every model has
dated rates, so a switch can be priced honestly before it happens rather than
discovered in next month's spend.

WHAT HAPPENS TO WORK ALREADY DONE. Not the same answer for every task, so it is
declared per task rather than assumed: these sweeps skip finished rows without
regard to which model did them, so a switch MIXES two models' answers in one
catalog and re-pays for nothing.

Capability stays hard and sanction stays soft: a model that cannot do the work
is not offered at all, because that failure would arrive mid-batch after the
money was spent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.routers.admin import require_admin
from api.tasks import SHAPES
from core import providers
from core.routing import NoEligibleModel, candidates_for, resolve

router = APIRouter(prefix="/admin")


# How many history rows a task view carries. Enough to see the last few
# decisions in context without turning a settings screen into a log viewer;
# the full history is its own endpoint.
RECENT_CHANGES = 10


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


def _history(purpose: str, limit: int) -> list[dict[str, Any]]:
    return db.query(
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


def _current(purpose: str) -> dict[str, Any] | None:
    rows = _history(purpose, 1)
    return rows[0] if rows else None


def _money(value: Any) -> str | None:
    """A dollar figure as a plain decimal string, or absent.

    Sent as a string rather than a float because these are Decimals and a
    float would round money on the way out. Formatted with 'f' because
    str(Decimal) renders an exact zero as "0E-8", which is a correct Decimal
    and an alarming thing to put on a spending screen.

    None stays None all the way to the client: an ineligible model has no
    price and an unbounded sweep has no cycle cost, and rendering either as 0
    would make an unknown look like a free one.
    """
    return format(value, "f") if value is not None else None


def _view(purpose: str) -> dict[str, Any]:
    shape = SHAPES[purpose]
    latest = _current(purpose)
    override = (latest or {}).get("model")
    candidacies = candidates_for(shape)
    current_model = override or (shape.candidates[0] if shape.candidates else None)
    current_cycle = next(
        (c.est_cycle_cost_usd for c in candidacies if c.model == current_model), None
    )
    try:
        chosen = resolve(shape, override=override)
        resolved: dict[str, Any] | None = {
            "model": chosen.model,
            "provider": chosen.provider,
            "reason": chosen.reason,
            "overridden": chosen.overridden,
            "est_cost_usd": str(chosen.est_cost_usd) if chosen.est_cost_usd else None,
            "params": {k: str(v) for k, v in chosen.params.items()},
        }
        error = None
    except NoEligibleModel as exc:
        # A configured model that stopped being able to do the work - a
        # datasheet changed, a provider dropped a capability. Reported rather
        # than raised: the screen exists to fix exactly this.
        resolved, error = None, str(exc)
    return {
        "purpose": purpose,
        "label": shape.label or purpose,
        "notes": shape.notes,
        "on_model_change": shape.on_model_change,
        "batched": shape.batched,
        "structured": shape.structured,
        "sanctioned": list(shape.candidates),
        "override": override,
        "override_is_outside_sanctioned": bool(override) and override not in shape.candidates,
        "resolved": resolved,
        "error": error,
        # Every declared model, eligible or not, each carrying WHY. A short
        # list with the impossible options silently removed is how someone
        # concludes the missing model is a bug; the rejection reason is the
        # useful half.
        "candidates": [
            {
                "model": c.model,
                "provider": c.provider,
                "eligible": c.eligible,
                "rejection": c.rejection,
                "sanctioned": c.sanctioned,
                "est_cost_usd": _money(c.est_cost_usd),
                "est_cycle_cost_usd": _money(c.est_cycle_cost_usd),
                # Against whatever runs today, so the screen can show the
                # consequence of THIS switch rather than an absolute nobody
                # has a reference for. Absent when either side is unknown.
                "est_cycle_cost_delta_usd": _money(
                    c.est_cycle_cost_usd - current_cycle
                    if c.est_cycle_cost_usd is not None and current_cycle is not None
                    else None
                ),
                # True when this model is billed by the hour of day, which
                # makes the figures above the peak ones. A caveat for beside
                # the price, not a state.
                "price_varies_by_time": c.price_varies_by_time,
                # The measured findings about THIS model on THIS task, so the
                # client renders them on the option they are about rather than
                # holding its own copy that rots at the next measurement.
                "evidence": [
                    {
                        "verdict": e.verdict,
                        "finding": e.finding,
                        "sample_size": e.sample_size,
                        "measured_on": e.measured_on.isoformat(),
                    }
                    for e in shape.evidence
                    if e.model == c.model
                ],
            }
            for c in candidacies
        ],
        # What a cycle cost is computed FROM, so the client can render it as an
        # estimate with its basis rather than as a price. per_cycle is the
        # handler's own cap; a task with no cap sends 0 and no cycle cost.
        "cost_basis": {
            "per_cycle": shape.per_cycle,
            "est_prompt_tokens": shape.est_prompt_tokens,
            "max_output_tokens": shape.max_output_tokens,
            "batched": shape.batched,
        },
        "recent_changes": _history(purpose, RECENT_CHANGES),
    }


def _require_cost_acknowledgement(purpose: str, shape, body: TaskModelPut) -> None:
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
    current_model = _current(purpose) or {}
    current_name = current_model.get("model") or (shape.candidates[0] if shape.candidates else None)
    current = candidacies.get(current_name or "")
    if new is None or current is None:
        return
    if new.est_cycle_cost_usd is None or not current.est_cycle_cost_usd:
        return
    multiple = new.est_cycle_cost_usd / current.est_cycle_cost_usd
    if multiple <= COST_ACKNOWLEDGEMENT_MULTIPLE:
        return
    raise HTTPException(
        400,
        detail={
            "code": "COST_ACKNOWLEDGEMENT_REQUIRED",
            "message": (
                f"{body.model} costs {multiple:.1f}x what {current_name} costs for this "
                f"task - {_money(new.est_cycle_cost_usd)} against "
                f"{_money(current.est_cycle_cost_usd)} per cycle. Re-send with "
                f"acknowledge_cost to proceed."
            ),
            "multiple": f"{multiple:.1f}",
            "current_model": current_name,
            "current_cycle_cost_usd": _money(current.est_cycle_cost_usd),
            "new_cycle_cost_usd": _money(new.est_cycle_cost_usd),
            "threshold": COST_ACKNOWLEDGEMENT_MULTIPLE,
        },
    )


@router.get("/task-models")
def list_task_models(_: AuthedUser = Depends(require_admin)):
    """Every configurable task, with its eligible models and what each costs.

    Costs are per CALL at the shape's own declared token profile, not per
    cycle. A cycle figure would need each handler's per-cycle cap, which lives
    in its own module and moves; multiplying a real number by a guessed one
    produces a confident wrong total, which is worse on a spending screen than
    an honest small one.
    """
    return {"tasks": [_view(p) for p in sorted(SHAPES)]}


@router.get("/task-models/{purpose}")
def get_task_model(purpose: str, _: AuthedUser = Depends(require_admin)):
    if purpose not in SHAPES:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown task"})
    return _view(purpose)


@router.put("/task-models/{purpose}")
def put_task_model(purpose: str, body: TaskModelPut, user: AuthedUser = Depends(require_admin)):
    """Set or clear the model for a task.

    Refuses a model the task's declared needs cannot admit - that is capability,
    and accepting it would move the failure to the middle of a paid batch.
    Accepts a model outside the sanctioned set, and records that it was one, so
    the row itself says an override was made rather than leaving it to be
    re-derived later against a sanctioned list that has since moved.
    """
    if purpose not in SHAPES:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown task"})
    shape = SHAPES[purpose]
    if body.model is not None:
        if providers.model(body.model) is None:
            raise HTTPException(
                400,
                detail={"code": "UNKNOWN_MODEL", "message": f"{body.model} is not declared"},
            )
        try:
            resolve(shape, override=body.model)
        except NoEligibleModel as exc:
            raise HTTPException(
                400, detail={"code": "INELIGIBLE_MODEL", "message": str(exc)}
            ) from exc
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
    return _view(purpose)


@router.get("/task-models/{purpose}/history")
def task_model_history(purpose: str, _: AuthedUser = Depends(require_admin)):
    """Every decision ever made for this task, newest first.

    The table is append-only and a cleared override is a NULL row rather than a
    deletion, so this is the whole record and not a reconstruction. A monthly
    review is looking for the switch that explains a regression noticed weeks
    later, which only works if the switch is still here.
    """
    if purpose not in SHAPES:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown task"})
    return {"purpose": purpose, "changes": _history(purpose, 200)}
