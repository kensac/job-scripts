"""Resolve a task's model from declared capability, quality, and price.

Callers declare candidate models and hard requirements in a TaskShape; this
module filters eligible candidates and picks the cheapest. Provider key
availability is checked at spend sites via `server_key`, not during resolution.

A failed call switches models only when its provider reported the account cannot
pay and an admin set a fallback chain for the purpose (core.fallback); the switch
is recorded on every row it writes.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from core import batch_capabilities, providers
from core.pricing import estimate_cost_usd, is_off_peak
from core.providers.spec import Model, StructuredOutput

# Weakest to strongest; a task declares the minimum mode it requires.
_MODE_RANK = {
    StructuredOutput.NONE: 0,
    StructuredOutput.JSON_OBJECT: 1,
    StructuredOutput.JSON_SCHEMA: 2,
}


class NoEligibleModel(LookupError):
    """Raised when no candidate satisfies the task, with per-candidate reasons."""


def server_key(provider: str) -> str:
    """Return the server API key for `provider` from its declared env var.

    Kept separate from `resolve()` so capability and effort queries work without
    provider credentials on the host.
    """
    known = providers.PROVIDERS.get(provider)
    return os.environ.get(known.api_key_env, "") if known else ""


class ModelChangeEffect(StrEnum):
    """What happens to work already done when this task's model changes.

    Declared rather than inferred, because it is a fact about the handler's
    candidate query and nothing here can see one. Getting it wrong on a screen
    is worse than omitting it: the two answers point in opposite directions.
    """

    # Skip-check ignores model: existing rows remain and new rows use the new model.
    MIXES = "mixes"
    # Skip-check scopes by model: existing rows are re-run unless grouped in verdict_model_equivalence.
    RERUNS = "reruns"


@dataclass(frozen=True)
class Evidence:
    """Measured finding for one model on a task, surfaced in the model picker."""

    model: str
    # "excluded" or "chosen".
    verdict: str
    finding: str
    sample_size: int
    measured_on: datetime.date


@dataclass(frozen=True)
class TaskShape:
    """Declared requirements, candidates, and metadata for one AI task."""

    # Stable ledger and registry key.
    purpose: str
    # Minimum structured-output guarantee required by the task.
    structured: StructuredOutput
    # Hard requirement: True requires a model with a supported batch transport.
    batched: bool
    max_output_tokens: int
    # Order-of-magnitude prompt size used only for candidate ranking and estimates.
    est_prompt_tokens: int
    candidates: tuple[str, ...]
    # Pinned effort value; rejected if outside the model's declared `accepts`.
    effort: str | None = None
    # Ordered fallback when `effort` is None: first accepted value wins, else model default.
    effort_preference: tuple[str, ...] = ()

    params: dict[str, object] = field(default_factory=dict)
    label: str = ""
    # Max requests per cycle for server-side budget projections (0 = unbounded).
    per_cycle: int = 0
    # Operator-facing rationale rendered in the model picker UI.
    notes: str = ""
    on_model_change: ModelChangeEffect = ModelChangeEffect.MIXES
    evidence: tuple[Evidence, ...] = ()

    def resolved_effort(self) -> str | None:
        """Effort this shape sends to its first candidate, without needing a key."""
        declared = providers.model(self.candidates[0]) if self.candidates else None
        if declared is None:
            return None
        return self.effort or _preferred_effort(self, declared)


@dataclass(frozen=True)
class Choice:
    """Resolved model, request parameters, cost estimate, and rationale."""

    provider: str
    model: str
    params: dict[str, object]
    est_cost_usd: Decimal | None
    off_peak: bool
    reason: str
    # True when a configured override selected a model outside `shape.candidates`.
    overridden: bool = False
    # Original routed model when this choice came from a credit-exhaustion fallback.
    fallback_from: str | None = None


def _rejection(shape: TaskShape, name: str, declared: Model | None, provider: str | None) -> str:
    """Return why `name` cannot run `shape`, or "" when eligible."""
    if declared is None or provider is None:
        return "not declared in any provider datasheet"
    if _MODE_RANK[declared.structured_output.mode] < _MODE_RANK[shape.structured]:
        return (
            f"declares {declared.structured_output.mode} output, "
            f"task needs at least {shape.structured}"
        )
    if shape.batched:
        reason = batch_capabilities.unavailable_reason(name)
        if reason:
            return reason
    if shape.effort is not None:
        if declared.reasoning.param is None:
            return "model does not support an effort parameter"
        accepts = declared.reasoning.accepts
        # Empty `accepts` means unenumerated; defer validation to the provider.
        if accepts and shape.effort not in accepts:
            return f"does not accept effort {shape.effort!r} (accepts {', '.join(accepts)})"
    if (
        declared.output.max_output_tokens is not None
        and shape.max_output_tokens > declared.output.max_output_tokens
    ):
        return (
            f"caps output at {declared.output.max_output_tokens} tokens, "
            f"task asks for {shape.max_output_tokens}"
        )
    if estimate_cost_usd(name, 1, 1) is None:
        return "has no published rates, so its spend could not be reported"
    return ""


def _preferred_effort(shape: TaskShape, declared: Model) -> str | None:
    """First value in `shape.effort_preference` accepted by `declared`, else its default.

    An empty `accepts` falls through to the declared default rather than sending
    an unverified preference.
    """
    if shape.effort_preference and declared.reasoning.accepts:
        for effort in shape.effort_preference:
            if effort in declared.reasoning.accepts:
                return effort
    return declared.reasoning.default


@dataclass(frozen=True)
class Candidacy:
    """Eligibility and estimated cost for one selectable model on a task."""

    model: str
    provider: str
    eligible: bool
    rejection: str
    sanctioned: bool
    est_cost_usd: Decimal | None
    # None when per_cycle is 0 (unbounded sweep): a cycle figure would be invented.
    est_cycle_cost_usd: Decimal | None
    # True when rates depend on time of day (estimates above use peak rates).
    price_varies_by_time: bool


def candidates_for(shape: TaskShape, at: datetime.datetime | None = None) -> list[Candidacy]:
    """Evaluate every selectable model for `shape`, cheapest eligible first, then
    ineligible alphabetically (ordered server-side since prices serialize as strings)."""
    out: list[Candidacy] = []
    for name, (provider, declared) in sorted(providers.MODELS.items()):
        if not declared.selectable:
            continue
        why = _rejection(shape, name, declared, provider)
        per_call = None if why else _estimate(shape, name, at)
        out.append(
            Candidacy(
                model=name,
                provider=provider,
                eligible=not why,
                rejection=why,
                sanctioned=name in shape.candidates,
                est_cost_usd=per_call,
                est_cycle_cost_usd=(
                    per_call * shape.per_cycle if per_call is not None and shape.per_cycle else None
                ),
                price_varies_by_time=bool(declared.rates.peak_windows),
            )
        )
    return sorted(
        out,
        key=lambda c: (
            not c.eligible,
            c.est_cost_usd if c.est_cost_usd is not None else Decimal("Infinity"),
            c.model,
        ),
    )


def _params_for(shape: TaskShape, declared: Model) -> dict[str, object]:
    """Request parameters implied by `shape` for `declared` (shared by resolve and override)."""
    params = dict(shape.params)
    if declared.reasoning.param:
        effort = shape.effort or _preferred_effort(shape, declared)
        if effort:
            params.setdefault(declared.reasoning.param, effort)
    params.setdefault("max_output_tokens", shape.max_output_tokens)
    return params


def _estimate(shape: TaskShape, name: str, at: datetime.datetime | None) -> Decimal | None:
    return estimate_cost_usd(
        name, shape.est_prompt_tokens, shape.max_output_tokens, batched=shape.batched, at=at
    )


def resolve(
    shape: TaskShape,
    at: datetime.datetime | None = None,
    override: str | None = None,
) -> Choice:
    """Return the cheapest eligible sanctioned model for `shape` (or `override`).

    Omitting `at` prices at peak rates rather than assuming an off-peak discount.
    """
    if not shape.candidates:
        raise NoEligibleModel("no candidate models were declared for this task")
    if override:
        return _resolve_override(shape, override, at)

    eligible: list[tuple[Decimal, str, str, Model]] = []
    rejected: list[str] = []
    for name in shape.candidates:
        declared = providers.model(name)
        provider = providers.provider_of(name)
        why = _rejection(shape, name, declared, provider)
        if why:
            rejected.append(f"{name}: {why}")
            continue
        assert provider is not None and declared is not None
        cost = _estimate(shape, name, at)
        assert cost is not None
        eligible.append((cost, name, provider, declared))

    if not eligible:
        raise NoEligibleModel("no declared model can run this task - " + "; ".join(rejected))

    # Ties preserve candidate order (`min` keeps the first equal key).
    cost, name, provider, declared = min(eligible, key=lambda e: e[0])
    off_peak = is_off_peak(declared.rates, at)

    if len(shape.candidates) == 1:
        reason = "the only model the caller sanctioned"
    else:
        others = ", ".join(n for _, n, _, _ in eligible if n != name)
        reason = f"cheapest of {len(eligible)} eligible ({others}) at the time of the call"
    if off_peak:
        reason += "; off-peak rate applies"
    if rejected:
        reason += f"; excluded {len(rejected)}"
    return Choice(
        provider=provider,
        model=name,
        params=_params_for(shape, declared),
        est_cost_usd=cost,
        off_peak=off_peak,
        reason=reason,
    )


def _resolve_override(shape: TaskShape, override: str, at: datetime.datetime | None) -> Choice:
    """Resolve an admin override: bypasses `shape.candidates` but still enforces capability."""
    declared = providers.model(override)
    provider = providers.provider_of(override)
    why = _rejection(shape, override, declared, provider)
    if why:
        raise NoEligibleModel(f"{override} cannot run this task - {why}")
    assert provider is not None and declared is not None
    sanctioned = override in shape.candidates
    return Choice(
        provider=provider,
        model=override,
        params=_params_for(shape, declared),
        est_cost_usd=_estimate(shape, override, at),
        off_peak=is_off_peak(declared.rates, at),
        reason=(
            "configured override"
            if sanctioned
            else "configured override, outside the models this call site sanctioned"
        ),
        overridden=not sanctioned,
    )
