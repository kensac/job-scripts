from __future__ import annotations

from decimal import Decimal
from typing import Any

from api import db


def fleet_cycle_cost_usd() -> Decimal:
    """Estimate one sweep of every configurable task at its currently configured model."""
    from api.task_config import configured_model, configured_shape
    from core.routing import NoEligibleModel, resolve
    from core.shapes import SHAPES

    total = Decimal(0)
    for purpose, declared_shape in SHAPES.items():
        shape = configured_shape(declared_shape)
        try:
            chosen = resolve(shape, override=configured_model(purpose))
        except NoEligibleModel:
            # Skip misconfigured tasks so they do not disable budget checks for the rest.
            continue
        if chosen.est_cost_usd is not None:
            total += chosen.est_cost_usd * shape.per_cycle
    return total


def fleet_spend_this_week() -> Decimal:
    """Fleet spend (`user_id IS NULL`) since the start of the current UTC week."""
    row = db.query_one(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM model_calls "
        "WHERE user_id IS NULL AND created_at >= date_trunc('week', now() AT TIME ZONE 'UTC')"
    )
    return Decimal(str((row or {}).get("spent") or 0))


class FleetBudgetExceeded(RuntimeError):
    """Raised when fleet spend meets or would exceed its weekly ceiling."""


def fleet_budget_status(projected_usd: Decimal | None = None) -> dict[str, Any]:
    """Return current weekly fleet spend, ceiling, headroom, and optional pre-submit projection."""
    cycle_cost = fleet_cycle_cost_usd()
    cycles = int(db.get_config("fleet_weekly_cycles"))
    ceiling = cycle_cost * cycles if cycles > 0 else Decimal(0)
    spent = fleet_spend_this_week()
    projected = spent + (projected_usd or Decimal(0))
    return {
        "enabled": cycles > 0 and ceiling > 0,
        "spent_usd": spent,
        "ceiling_usd": ceiling,
        "cycles": cycles,
        "cycle_cost_usd": cycle_cost,
        "headroom_usd": ceiling - spent if ceiling > 0 else None,
        "used_fraction": (spent / ceiling) if ceiling > 0 else None,
        "projected_usd": projected if projected_usd is not None else None,
        "projected_exceeds": bool(ceiling > 0 and projected > ceiling),
        # Return ceiling coverage limits as data so UI surfaces do not imply
        # this bounds external spend on a shared provider key.
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
    """Raise FleetBudgetExceeded before submitting work that would breach the weekly ceiling.

    A zero `fleet_weekly_cycles` disables the check for intentional backfills.
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
