from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import budget, model_calls, user_settings
from api.auth import AuthedUser, is_admin, require_user
from api.routers.users_routes.settings import MAX_AGE_CAP_DAYS

router = APIRouter()


class GrantLimits(BaseModel):
    enabled_filters: int
    max_age_days: int | None


class Grants(BaseModel):
    """What this person may spend and on what, in one place."""

    owner_key: bool
    weekly_token_budget: int | None
    spent_this_week: int
    has_byo_key: bool
    key_source: str | None
    owner_key_models: list[str]
    # The other two caps a person runs under, so the Usage page states
    # every limit in one place rather than each surfacing as a refusal.
    limits: GrantLimits


class BootstrappedUser(BaseModel):
    """Who the caller is, as the session already knows them. Empty rather than
    null where the identity provider sent nothing, which is what AuthedUser
    already holds."""

    id: int
    sub: str
    email: str
    name: str


class Bootstrap(BaseModel):
    user: BootstrappedUser
    grants: Grants


# A sum over a bigint column is numeric in Postgres, so every figure below
# arrives as a Decimal. Token counts are whole and declared int; money is
# declared float, which is what a Decimal already became on the way out.
class UserSpendDay(BaseModel):
    day: datetime.date
    # Null: the call's record did not say whose key (live filter answers
    # before 2026-09-13).
    key_source: str | None
    tokens: int
    calls: int
    cost_usd: float
    unpriced_calls: int


class UserSpendPurpose(BaseModel):
    purpose: str
    model: str | None
    tokens: int
    calls: int
    cost_usd: float
    cached_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_calls: int
    unpriced_calls: int


class Usage(Grants):
    """The grants, with the spend they have been put to."""

    spend_by_day: list[UserSpendDay]
    spend_by_purpose: list[UserSpendPurpose]


def _grants(user: AuthedUser) -> Grants:
    ent = budget.get_entitlement(user)
    return Grants(
        owner_key=ent.owner_key,
        weekly_token_budget=ent.weekly_token_budget,
        spent_this_week=ent.spent_this_week,
        has_byo_key=ent.has_byo_key,
        key_source=ent.key_source,
        owner_key_models=budget.owner_allowed_models(user.groups) if ent.owner_key else [],
        limits=GrantLimits(
            enabled_filters=1,
            max_age_days=None if is_admin(user.groups) else MAX_AGE_CAP_DAYS,
        ),
    )


@router.post("/users/bootstrap")
def bootstrap(user: AuthedUser = Depends(require_user)) -> Bootstrap:
    user_settings.ensure(user.id)
    return Bootstrap(
        user=BootstrappedUser(id=user.id, sub=user.sub, email=user.email, name=user.name),
        grants=_grants(user),
    )


@router.get("/user/usage")
def usage(user: AuthedUser = Depends(require_user)) -> Usage:
    """Grants, plus the spend history the admin view already computed.

    These are the same two aggregates /admin/users/{id} runs, scoped to the
    caller. They existed for months and only an admin could see them, which is
    why a user's own Usage page had nothing to show.
    """
    return Usage(
        **_grants(user).model_dump(),
        spend_by_day=[UserSpendDay(**r) for r in model_calls.user_spend_by_day(user.id)],
        spend_by_purpose=[
            UserSpendPurpose(**r) for r in model_calls.user_spend_by_purpose(user.id)
        ],
    )


# Derived from the datasheets rather than listed here. This was a hardcoded
# map of three providers and it never gained xai or deepseek, so adding them to
# the picker would have raised KeyError on a page that only ever showed two.
# The reasoning parameter is per MODEL, not per provider - xAI accepts it on
# some of its models and rejects it on others - so the provider offers the
# union and validate_params still checks the specific model.
