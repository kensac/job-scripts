from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from api import crypto, db, model_calls, user_settings
from api.auth import AuthedUser
from core import providers, routing

if TYPE_CHECKING:
    from api import ai


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
        "spent this week. It resets weekly. To keep going now, add your own API key under "
        "AI & keys; your own key has no cap and is billed to you."
    )


def access_failure(user: AuthedUser) -> AIAccessError | None:
    try:
        resolve_ai_config(user.id, get_entitlement(user))
    except AIAccessError as exc:
        return exc
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


def owner_budget(groups: list[str]) -> tuple[bool, int | None]:
    """Whether these groups have server credits and their shared weekly cap."""
    return _owner_budget(groups)


def spent_this_week(user_id: int) -> int:
    row = db.query_one(
        "SELECT COALESCE(SUM(total_tokens), 0) AS spent FROM model_calls "
        "WHERE user_id = %s AND key_source = 'owner' "
        "AND created_at >= now() - interval '7 days'",
        (user_id,),
    )
    return int(row["spent"]) if row else 0


def get_entitlement(user: AuthedUser) -> Entitlement:
    owner, weekly = _owner_budget(user.groups)
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
    allowed: set = set()
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
    if entitlement.owner_key:
        if (
            entitlement.weekly_token_budget is not None
            and entitlement.spent_this_week >= entitlement.weekly_token_budget
        ):
            raise AIBudgetExceeded(BUDGET_EXCEEDED, entitlement)
        allowed = owner_allowed_models(entitlement.groups or [])
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
        if model:
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
    raise AIConfigUnavailable("NO_API_KEY", entitlement)


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
    managed boards included, as the old usage ledger counted it. A call with
    no recorded payer is older than any week this is asked about.
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
    backfill needs "how much room is left", which is a different question and
    the one nothing could answer: the ceiling existed only inside the check
    that enforced it, so the first time anyone saw it was when work stopped.

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


def record_tokens(
    user_id: int,
    key_source: str,
    purpose: str,
    model: str | None,
    usage: Mapping[str, int | None],
    *,
    batched: bool = False,
) -> None:
    """A person's call. A live one is written to the call ledger here; a
    batched one is already there, written with its receipt by the checkpoint,
    so its consumer's booking only counts its tokens."""
    _book(Booking(model_calls.Payer(user_id=user_id), key_source, purpose), model, usage, batched)


@dataclass(frozen=True)
class Booking:
    """Who a call is charged to, on whose key, and for what."""

    payer: model_calls.Payer
    key_source: str
    purpose: str


def book_live(
    booking: Booking,
    model: str | None,
    usage: Mapping[str, int | None],
    duration_ms: int | None = None,
) -> int | None:
    """Write a live call to the ledger and count its tokens; its id, for the
    answer it produced to point at, or None when nothing was billed."""
    return _book(booking, model, usage, False, duration_ms)


def _book(
    booking: Booking,
    model: str | None,
    usage: Mapping[str, int | None],
    batched: bool,
    duration_ms: int | None = None,
) -> int | None:
    total = usage.get("total_tokens")
    if not total:
        return None
    call_id = (
        None
        if batched
        else model_calls.record_live(
            model_calls.Call(
                booking.purpose,
                model,
                booking.payer,
                booking.key_source,
                usage,
                duration_ms=duration_ms,
            )
        )
    )
    from api import metrics

    metrics.AI_TOKENS.labels(booking.key_source, booking.purpose).inc(total)
    return call_id


def record_managed_board_tokens(
    managed_board_id: int,
    purpose: str,
    model: str | None,
    usage: Mapping[str, int | None],
    *,
    batched: bool = False,
) -> None:
    """Server-key work owned by a managed board, never a fake user."""
    _book(
        Booking(model_calls.Payer(managed_board_id=managed_board_id), "owner", purpose),
        model,
        usage,
        batched,
    )


@contextmanager
def record_parse_failures(user_id: int, key_source: str, purpose: str, model: str | None):
    from api.ai import PaidParseError

    try:
        yield
    except PaidParseError as exc:
        record_tokens(user_id, key_source, purpose, model, exc.usage)
        raise


def load_config(user_id: int, ignore_budget: bool = False) -> tuple[Entitlement, ai.AIConfig]:
    """The person's entitlement and model config for a task. With
    ignore_budget the shared weekly cap is lifted for this task only (an
    admin queued it that way): the spend is still recorded, the cap itself
    does not move (Kanishk, 2026-09-08: raising it and putting it back for
    one run was the wrong tool)."""
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
