"""Who may spend on AI, and where spend is counted.

This module resolves a person's entitlement and model config. The fleet's
weekly ceiling lives in `api.budget.fleet`, and the ledger writes in
`api.budget.booking`; both are re-exported here, so callers import `api.budget`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from api import crypto, db, user_settings
from api.budget.booking import Booking as Booking
from api.budget.booking import book_live as book_live
from api.budget.booking import record_managed_board_tokens as record_managed_board_tokens
from api.budget.booking import record_parse_failures as record_parse_failures
from api.budget.booking import record_tokens as record_tokens
from api.budget.fleet import FleetBudgetExceeded as FleetBudgetExceeded
from api.budget.fleet import check_fleet_budget as check_fleet_budget
from api.budget.fleet import fleet_budget_status as fleet_budget_status
from api.budget.fleet import fleet_cycle_cost_usd as fleet_cycle_cost_usd
from api.budget.fleet import fleet_spend_this_week as fleet_spend_this_week
from core import providers, routing
lazy from api.auth import AuthedUser

if TYPE_CHECKING:
    from api import ai


@dataclass
class Entitlement:
    owner_key: bool
    weekly_token_budget: int | None
    spent_this_week: int
    has_byo_key: bool
    groups: list[str]

    @property
    def budget_exhausted(self) -> bool:
        return (
            self.weekly_token_budget is not None
            and self.spent_this_week >= self.weekly_token_budget
        )

    @property
    def key_source(self) -> str | None:
        if self.has_byo_key:
            return "byo"
        if self.owner_key and not self.budget_exhausted:
            return "owner"
        return None


AccessReason = Literal["NO_API_KEY", "NO_MODEL", "BUDGET_EXCEEDED"]
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class AIAccessError(Exception):
    def __init__(self, reason: AccessReason, entitlement: Entitlement):
        super().__init__(reason)
        self.reason: AccessReason = reason
        self.entitlement = entitlement

    @property
    def message(self) -> str:
        return access_message(self.reason, self.entitlement)


class AIConfigUnavailable(AIAccessError, LookupError):
    pass


class AIBudgetExceeded(AIAccessError, PermissionError):
    pass


def access_message(reason: AccessReason, ent: Entitlement) -> str:
    if reason == "NO_MODEL":
        return "Choose a model for your provider under AI & keys."
    if reason == "NO_API_KEY":
        return "There is no available key to run on. Add your own API key under AI & keys."
    cap = ent.weekly_token_budget or 0
    return (
        f"The shared weekly AI budget is used up: {ent.spent_this_week:,} of {cap:,} tokens "
        "counted over the last seven days. Tokens free up as older use passes seven days. "
        "To keep going now, add your own API key under AI & keys; your own key has no cap "
        "and is billed to you."
    )


def access_failure(user: AuthedUser) -> AIAccessError | None:
    try:
        resolve_ai_config(user.id, get_entitlement(user))
    except AIAccessError as exc:
        return exc
    return None


def owner_budget(groups: list[str]) -> tuple[bool, int | None]:
    """Whether these groups have server credits and their shared weekly cap."""
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
        "SELECT COALESCE(SUM(total_tokens), 0) AS spent FROM model_calls "
        "WHERE user_id = %s AND key_source = 'owner' "
        "AND created_at >= now() - interval '7 days'",
        (user_id,),
    )
    return int(row["spent"]) if row else 0


def get_entitlement(user: AuthedUser) -> Entitlement:
    owner, weekly = owner_budget(user.groups)
    return Entitlement(
        owner_key=owner,
        weekly_token_budget=weekly,
        spent_this_week=spent_this_week(user.id) if owner else 0,
        has_byo_key=user_settings.has_own_key(user.id),
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
    allowed: set[str] = set()
    for r in rows:
        if r["allowed_models"] is not None:
            allowed |= set(r["allowed_models"])
        else:
            allowed |= set(ai.owner_models(r["weekly_token_budget"] is None))
    keyed = {
        m["model"]
        for provider, models in ai.MODEL_CATALOG.items()
        if routing.server_key(provider)
        for m in models
    }
    return sorted(allowed & keyed)


def resolve_ai_config(user_id: int, entitlement: Entitlement):
    """Resolve the effective model and credentials or a typed access refusal."""

    from api import ai

    creds = user_settings.credentials(user_id)
    params = creds.ai_params

    if entitlement.has_byo_key and creds.api_key_enc:
        provider = creds.ai_provider
        model = creds.ai_model or ai.DEFAULT_MODELS.get(provider)
        if not model:
            raise AIConfigUnavailable("NO_MODEL", entitlement)
        return ai.AIConfig(
            provider=provider,
            api_key=crypto.decrypt(creds.api_key_enc),
            key_source="byo",
            model=model,
            base_url=creds.ai_base_url,
            params=params,
        )
    if not entitlement.owner_key:
        raise AIConfigUnavailable("NO_API_KEY", entitlement)
    if entitlement.budget_exhausted:
        raise AIBudgetExceeded(BUDGET_EXCEEDED, entitlement)
    allowed = owner_allowed_models(entitlement.groups)
    chosen = creds.ai_model
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
    if not model:
        raise AIConfigUnavailable("NO_API_KEY", entitlement)
    provider = providers.provider_of(model) or "openai"
    return ai.AIConfig(
        provider=provider,
        api_key=routing.server_key(provider),
        key_source="owner",
        model=model,
        params={k: v for k, v in params.items() if k != "temperature"},
        substituted_from=substituted_from,
        substitution_reason=reason,
    )


def load_config(user_id: int, ignore_budget: bool = False) -> tuple[Entitlement, ai.AIConfig]:
    """The person's entitlement and model config for a task. With
    ignore_budget the shared weekly cap is lifted for this task only (a
    hand-written task row, or a managed board's sponsor): the spend is still
    recorded, the cap itself does not move (Kanishk, 2026-09-08: raising it
    and putting it back for one run was the wrong tool)."""
    user = db.query_one("SELECT id, sub, email, name, groups FROM users WHERE id = %s", (user_id,))
    if not user:
        raise LookupError("unknown user")
    authed = AuthedUser(
        id=user["id"],
        sub=user["sub"],
        email=user["email"] or "",
        name=user["name"] or "",
        groups=user["groups"] or [],
    )
    ent = get_entitlement(authed)
    if ignore_budget and ent.owner_key:
        ent = dataclasses.replace(ent, weekly_token_budget=None)
    return ent, resolve_ai_config(user_id, ent)
