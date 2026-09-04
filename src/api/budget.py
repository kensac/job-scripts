from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from api import crypto, db
from api.auth import AuthedUser
from core import pricing


@dataclass
class Entitlement:
    owner_key: bool
    weekly_token_budget: int | None
    spent_this_week: int
    has_byo_key: bool
    groups: list[str] = None  # type: ignore[assignment]

    @property
    def key_source(self) -> str | None:
        if self.has_byo_key:
            return "byo"
        if self.owner_key and (
            self.weekly_token_budget is None or self.spent_this_week < self.weekly_token_budget
        ):
            return "owner"
        return None


def _owner_budget(groups: list[str]) -> tuple[bool, int | None]:
    if not groups:
        return False, None
    rows = db.query(
        "SELECT weekly_token_budget FROM group_budgets WHERE group_name = ANY(%s)",
        (groups,),
    )
    if not rows:
        return False, None
    budgets = [r["weekly_token_budget"] for r in rows]
    if any(b is None for b in budgets):
        return True, None
    return True, max(budgets)


def spent_this_week(user_id: int) -> int:
    row = db.query_one(
        "SELECT COALESCE(SUM(total_tokens), 0) AS spent FROM api_usage "
        "WHERE user_id = %s AND key_source = 'owner' "
        "AND created_at >= now() - interval '7 days'",
        (user_id,),
    )
    return int(row["spent"]) if row else 0


def get_entitlement(user: AuthedUser) -> Entitlement:
    owner, weekly = _owner_budget(user.groups)
    settings = db.query_one(
        "SELECT api_key_enc IS NOT NULL AS has_key FROM user_settings WHERE user_id = %s",
        (user.id,),
    )
    return Entitlement(
        owner_key=owner,
        weekly_token_budget=weekly,
        spent_this_week=spent_this_week(user.id) if owner else 0,
        has_byo_key=bool(settings and settings["has_key"]),
        groups=list(user.groups),
    )


def owner_allowed_models(groups: list[str]) -> list[str]:
    """Models this user may run on the owner key: union across their tiers.
    A tier with an explicit allowed_models list grants exactly those; a NULL
    tier grants the default policy for its budget class. Everything is
    intersected with what the server actually has keys for."""
    from api import ai

    if not groups:
        return []
    rows = db.query(
        "SELECT weekly_token_budget, allowed_models FROM group_budgets WHERE group_name = ANY(%s)",
        (groups,),
    )
    allowed: set = set()
    for r in rows:
        if r["allowed_models"] is not None:
            allowed |= set(r["allowed_models"])
        else:
            allowed |= set(ai.owner_models(r["weekly_token_budget"] is None))
    keyed = {
        m["model"]
        for provider, models in ai.MODEL_CATALOG.items()
        if ai.server_key(provider)
        for m in models
    }
    return sorted(allowed & keyed)


def resolve_ai_config(user_id: int, entitlement: Entitlement):
    """Returns an ai.AIConfig or raises LookupError / PermissionError."""

    from api import ai

    settings = (
        db.query_one(
            "SELECT api_key_enc, ai_provider, ai_base_url, ai_model, ai_params "
            "FROM user_settings WHERE user_id = %s",
            (user_id,),
        )
        or {}
    )
    params = settings.get("ai_params") or {}

    if entitlement.has_byo_key and settings.get("api_key_enc"):
        provider = settings.get("ai_provider") or "openai"
        model = settings.get("ai_model") or ai.DEFAULT_MODELS.get(provider)
        if not model:
            raise LookupError("NO_MODEL")
        return ai.AIConfig(
            provider=provider,
            api_key=crypto.decrypt(settings["api_key_enc"]),
            key_source="byo",
            model=model,
            base_url=settings.get("ai_base_url"),
            params=params,
        )
    if entitlement.owner_key:
        if (
            entitlement.weekly_token_budget is not None
            and entitlement.spent_this_week >= entitlement.weekly_token_budget
        ):
            raise PermissionError("BUDGET_EXCEEDED")
        allowed = owner_allowed_models(entitlement.groups or [])
        chosen = settings.get("ai_model")
        model = chosen
        substituted_from = reason = None
        if model not in allowed:
            model = (
                ai.DEFAULT_MODELS["openai"]
                if ai.DEFAULT_MODELS["openai"] in allowed
                else (allowed[0] if allowed else None)
            )
            # Only a real substitution is reported. A user who never chose a
            # model has not had one taken away, and saying so would put a
            # correction on a screen where nothing was corrected.
            if chosen:
                substituted_from = chosen
                reason = (
                    f"{chosen} is not available on shared credits; "
                    f"{model} was used instead. Add your own API key to choose freely."
                )
        if model:
            provider = ai.provider_of_model(model) or "openai"
            return ai.AIConfig(
                provider=provider,
                api_key=ai.server_key(provider),
                key_source="owner",
                model=model,
                params={k: v for k, v in params.items() if k != "temperature"},
                substituted_from=substituted_from,
                substitution_reason=reason,
            )
    raise LookupError("NO_API_KEY")


# A week's fleet ceiling, expressed as "this many full sweeps of every task at
# its sanctioned model" rather than as a dollar figure.
#
# Dollars would need re-picking every time a task is added or a model changes.
# This moves with the design: the fleet's sanctioned cost for one cycle of
# everything is computable from the shapes themselves, and the ceiling is a
# multiple of it.
#
# 24 is derived from observation. One full sweep of every task at its
# sanctioned models is $11.71; the fleet actually spent $29.19 in its busiest
# week, which is about 2.5 sweeps' worth, because sweeps mostly find nothing to
# do. 24 leaves roughly ten times the observed headroom - high enough that
# growth and a backfill do not trip it, low enough that the runaway this exists
# to catch does. A single task switched to the dearest model it can reach costs
# $30 an hour, which breaches this inside a day.
FLEET_WEEKLY_CYCLES = int(os.environ.get("JOBTRACKER_FLEET_WEEKLY_CYCLES", "24"))


def fleet_cycle_cost_usd() -> Decimal:
    """What one sweep of every configurable task costs at its CURRENT model.

    Current, not sanctioned: an override is part of what the fleet now costs,
    and a ceiling that ignored overrides would be measuring a fleet that is not
    running.
    """
    from api.tasks import SHAPES
    from api.tasks.runtime import configured_model
    from core.routing import NoEligibleModel, resolve

    total = Decimal(0)
    for purpose, shape in SHAPES.items():
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

    user_id IS NULL is what makes a row fleet work rather than a person's -
    the same predicate record_fleet_usage writes.
    """
    row = db.query_one(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM api_usage "
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
    backfill needs "how much room is left", which is a different question and
    the one nothing could answer: the ceiling existed only inside the check
    that enforced it, so the first time anyone saw it was when work stopped.

    `projected_usd` is what a pending submission would add, so the caller can
    ask the question before committing rather than after.
    """
    cycle_cost = fleet_cycle_cost_usd()
    ceiling = cycle_cost * FLEET_WEEKLY_CYCLES if FLEET_WEEKLY_CYCLES > 0 else Decimal(0)
    spent = fleet_spend_this_week()
    projected = spent + (projected_usd or Decimal(0))
    return {
        "enabled": FLEET_WEEKLY_CYCLES > 0 and ceiling > 0,
        "spent_usd": spent,
        "ceiling_usd": ceiling,
        # Expressed in sweeps as well as dollars, because that is the unit the
        # ceiling is actually defined in - a dollar figure alone invites
        # someone to change it to a rounder number and lose the derivation.
        "cycles": FLEET_WEEKLY_CYCLES,
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
            f"${ceiling:.2f} ({FLEET_WEEKLY_CYCLES} full sweeps at current models); "
            f"raise JOBTRACKER_FLEET_WEEKLY_CYCLES or wait for the week to roll over"
        )


def record_fleet_usage(
    purpose: str,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    batched: bool = True,
) -> None:
    """Scheduled work, charged to the fleet rather than to a person.

    `api_usage` is the ledger of record for spend, and until now it held only
    the sync path - so the largest line item in the system was invisible to it.
    Mail classification alone is $18.49 of batched work that the spend page,
    which reads ai_queries, could not see at all because message
    classification is not URL-keyed and writes no verdict row.

    This is called from the ONE place every batched caller already passes
    through, with the purpose it already declares. That is what makes a new AI
    caller appear in analytics without anyone remembering to wire it up: the
    hook cannot be used without naming a purpose, and naming a purpose is all
    the reporting needs.

    user_id is NULL on purpose. Catalog-wide extraction belongs to nobody in
    particular, and attributing it to whichever admin happens to be user 1
    would make per-user spend a fiction.
    """
    db.execute(
        "INSERT INTO api_usage (user_id, key_source, purpose, model, prompt_tokens, "
        "completion_tokens, total_tokens, cached_tokens, batched, cost_usd) "
        "VALUES (NULL, 'server', %s, %s, %s, %s, %s, 0, %s, %s)",
        (
            purpose,
            model,
            prompt_tokens,
            completion_tokens,
            prompt_tokens + completion_tokens,
            batched,
            pricing.estimate_cost_usd(model, prompt_tokens, completion_tokens, batched=batched),
        ),
    )


def record_usage(
    user_id: int,
    key_source: str,
    purpose: str,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cached_tokens: int = 0,
) -> None:
    """Every call charged to a user. record_usage is the sync path only - work
    that a human waits on - so it never carries the batch discount."""
    db.execute(
        "INSERT INTO api_usage (user_id, key_source, purpose, model, "
        "prompt_tokens, completion_tokens, total_tokens, cached_tokens, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            user_id,
            key_source,
            purpose,
            model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_tokens,
            pricing.estimate_cost_usd(
                model, prompt_tokens, completion_tokens, cached_tokens=cached_tokens
            ),
        ),
    )
    from api import metrics

    metrics.AI_TOKENS.labels(key_source, purpose).inc(total_tokens)
