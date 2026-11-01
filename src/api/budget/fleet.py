from __future__ import annotations

from decimal import Decimal
from typing import Any

from api import db


def fleet_cycle_cost_usd() -> Decimal:
    """What one sweep of every configurable task costs at its CURRENT model.

    Current, not sanctioned: an override is part of what the fleet now costs,
    and a ceiling that ignored overrides would be measuring a fleet that is not
    running.
    """
    from api.task_config import configured_model, configured_shape
    from core.routing import NoEligibleModel, resolve
    from core.shapes import SHAPES

    total = Decimal(0)
    for purpose, declared_shape in SHAPES.items():
        shape = configured_shape(declared_shape)
        try:
            chosen = resolve(shape, override=configured_model(purpose))
        except NoEligibleModel:
            # A task that cannot run costs nothing, and refusing to compute a
            # ceiling because one task is misconfigured would take down the
            # control for every other task.
            continue
        if chosen.est_cost_usd is not None:
            total += chosen.est_cost_usd * shape.per_cycle
    return total


def fleet_spend_this_week() -> Decimal:
    """Fleet spend since the start of the current week, in UTC.

    user_id IS NULL is what makes a call fleet work rather than a person's,
    managed boards included. A call with no recorded payer is older than any
    week this is asked about.
    """
    row = db.query_one(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM model_calls "
        "WHERE user_id IS NULL AND created_at >= date_trunc('week', now() AT TIME ZONE 'UTC')"
    )
    return Decimal(str((row or {}).get("spent") or 0))


class FleetBudgetExceeded(RuntimeError):
    """The fleet has spent past its weekly ceiling.

    Raised rather than logged. A warning would be seen by nobody at 3am, and
    the whole point is that the runaway case is one nobody is watching.
    """


def fleet_budget_status(projected_usd: Decimal | None = None) -> dict[str, Any]:
    """Where fleet spend sits against its ceiling, as a position rather than a
    total.

    A total answers "what have I spent". A person deciding whether to start a
    backfill needs "how much room is left".

    `projected_usd` is what a pending submission would add, so the caller can
    ask the question before committing rather than after.
    """
    cycle_cost = fleet_cycle_cost_usd()
    cycles = int(db.get_config("fleet_weekly_cycles"))
    ceiling = cycle_cost * cycles if cycles > 0 else Decimal(0)
    spent = fleet_spend_this_week()
    projected = spent + (projected_usd or Decimal(0))
    return {
        "enabled": cycles > 0 and ceiling > 0,
        "spent_usd": spent,
        "ceiling_usd": ceiling,
        # Expressed in sweeps as well as dollars, because that is the unit the
        # ceiling is actually defined in - a dollar figure alone invites
        # someone to change it to a rounder number and lose the derivation.
        "cycles": cycles,
        "cycle_cost_usd": cycle_cost,
        "headroom_usd": ceiling - spent if ceiling > 0 else None,
        "used_fraction": (spent / ceiling) if ceiling > 0 else None,
        "projected_usd": projected if projected_usd is not None else None,
        "projected_exceeds": bool(ceiling > 0 and projected > ceiling),
        # WHAT THIS CEILING DOES NOT COVER, carried as data rather than left in
        # a comment, because a budget screen that silently excludes part of the
        # spend is honest sentence by sentence and misleading as a whole.
        #
        # It counts calls this application recorded, and that is the whole of
        # what it can count. Spend on the same provider account that this
        # application did not make is invisible here by construction, whoever
        # made it - so this is a ceiling on what jobtracker spends, never a
        # ceiling on the bill.
        #
        # scope is a property of the code and is always true. excludes is a
        # property of the DEPLOYMENT: as of 2026-09-03 the OPENAI_API_KEY is
        # deliberately shared with karakeep and wardrowbe, with prefixed copies
        # in Infisical for the remote workers. This process cannot verify that
        # - it has one key and no way to ask who else holds it - so it is
        # stated as the possibility it is rather than asserted as fact. If the
        # keys are ever split, scope stays true and only the possibility goes
        # away.
        "scope": "calls recorded by this application",
        "excludes": (
            "any spend on the same provider account that this application did "
            "not make, which is invisible here whoever made it"
        ),
        "shared_key_possible": True,
        "shared_key_note": (
            "the provider key was deliberately shared with other fleet services "
            "as of 2026-09-03; this process cannot confirm that from here"
        ),
    }


def check_fleet_budget(projected_usd: Decimal | None = None) -> None:
    """Refuse to start more paid work the week's ceiling cannot cover.

    Checked before a batch is submitted rather than after, because a submitted
    batch is already billable - the provider has it, and cancelling is not
    something this system can rely on.

    `projected_usd` is what THIS submission will cost, and including it is the
    difference between a ceiling and a receipt. Without it the check compared
    only spend already made, so a single large batch sailed through at 12% of
    the ceiling and the refusal arrived on the next cycle, after the money was
    gone. That is the shape this control existed to prevent, reproduced inside
    the control itself.

    A ceiling of zero disables the check, which is how a deliberate backfill
    gets run without editing code.
    """
    status = fleet_budget_status(projected_usd)
    if not status["enabled"]:
        return
    spent, ceiling = status["spent_usd"], status["ceiling_usd"]
    if status["projected_exceeds"] or spent >= ceiling:
        projected = status["projected_usd"]
        detail = (
            f" and this submission would add ${projected - spent:.2f}"
            if projected is not None and projected > spent
            else ""
        )
        raise FleetBudgetExceeded(
            f"fleet spend this week is ${spent:.2f}{detail}, against a ceiling of "
            f"${ceiling:.2f} ({status['cycles']} full sweeps at current models); "
            f"raise fleet_weekly_cycles in the admin config or wait for the week to roll over"
        )
