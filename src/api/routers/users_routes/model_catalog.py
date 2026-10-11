from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import ai, budget, user_settings
from api.auth import AuthedUser, require_user
from core import providers as core_providers
from core import routing

router = APIRouter()


def _provider_params(provider: str) -> list[str]:
    known = core_providers.PROVIDERS.get(provider)
    if known is None:
        # openai_compatible is a user-supplied endpoint with no datasheet: we
        # do not know what it accepts, so offer the portable minimum.
        return ["temperature", "max_output_tokens"]
    params = {m.reasoning.param for m in known.models if m.reasoning.param}
    if known.supports_temperature:
        params.add("temperature")
    return [*sorted(params), "max_output_tokens"]


class ModelEntry(BaseModel):
    model: str
    note: str
    # What the datasheet declares. The page showed a name and a note while the
    # provider modules carried rates, context, batch eligibility and which
    # reasoning values each model accepts - so choosing a model meant guessing
    # at everything that distinguishes one from another.
    context_tokens: int | None = None
    rate_in_per_mtok: float | None = None
    rate_out_per_mtok: float | None = None
    batch_discount: float | None = None
    vendor_batch_supported: bool | None = None
    batch_available: bool = False
    batch_unavailable_reason: str | None = None
    rate_cached_in_per_mtok: float | None = None
    cache_write_5m_per_mtok: float | None = None
    cache_write_1h_per_mtok: float | None = None
    max_output_tokens: int | None = None
    pricing_source_url: str | None = None
    pricing_verified_on: str | None = None
    structured_output: str | None = None
    reasoning_accepts: list[str] = []
    # Whether THIS caller can run it right now, and if not, why, in words the
    # page can print. A model the caller cannot run is listed rather than
    # hidden, so the page can show what exists and what would unlock it.
    eligible: bool = True
    reason: str | None = None


class ProviderEntry(BaseModel):
    provider: str
    default_model: str | None
    models: list[ModelEntry]
    params: list[str]


class ModelsResponse(BaseModel):
    providers: list[ProviderEntry]
    owner_key_models: list[str]
    key_source: str | None
    addable_providers: list[str]


def _catalog(provider: str) -> list[dict]:
    """The provider's selectable models, carrying what their datasheet says.

    None stays None throughout: a rate nobody has looked up is not zero, and a
    context window nobody has recorded is not unlimited. The page renders the
    gap rather than a confident wrong number.
    """
    from core import batch_capabilities

    known = core_providers.PROVIDERS.get(provider)
    if known is None:
        return list(ai.MODEL_CATALOG.get(provider, []))
    out = []
    for m in known.models:
        if not m.selectable:
            continue
        tier = m.rates.tiers[0] if m.rates.tiers else None
        out.append(
            {
                "model": m.name,
                "note": m.note,
                "context_tokens": m.context_tokens,
                "max_output_tokens": m.output.max_output_tokens,
                "vendor_batch_supported": batch_capabilities.vendor_support(m.name),
                "batch_available": batch_capabilities.unavailable_reason(m.name) is None,
                "batch_unavailable_reason": batch_capabilities.unavailable_reason(m.name),
                "rate_cached_in_per_mtok": float(tier.rate_cached_in)
                if tier and tier.rate_cached_in is not None
                else None,
                "cache_write_5m_per_mtok": float(m.cache_write_5m_per_mtok)
                if m.cache_write_5m_per_mtok is not None
                else None,
                "cache_write_1h_per_mtok": float(m.cache_write_1h_per_mtok)
                if m.cache_write_1h_per_mtok is not None
                else None,
                "pricing_source_url": m.rates.source.url or None,
                "pricing_verified_on": m.rates.source.read_on.isoformat()
                if m.rates.source.read_on
                else None,
                "rate_in_per_mtok": float(tier.rate_in) if tier else None,
                "rate_out_per_mtok": float(tier.rate_out) if tier else None,
                "batch_discount": float(m.rates.batch_rate)
                if m.rates.batch_rate is not None
                else None,
                "structured_output": m.structured_output.mode.value
                if m.structured_output.mode
                else None,
                "reasoning_accepts": list(m.reasoning.accepts),
            }
        )
    return out


def _provider_entry(provider: str, models: list) -> dict:
    return {
        "provider": provider,
        "default_model": ai.DEFAULT_MODELS[provider],
        "models": models,
        "params": _provider_params(provider),
    }


NEEDS_OWN_KEY = "needs your own key"
NOT_ON_ALLOWLIST = "not on the shared-key allowlist"


@router.get("/models", response_model=ModelsResponse)
def models(user: AuthedUser = Depends(require_user)):
    """Every model the fleet knows, each marked with whether THIS user can run
    it right now and, if not, why. A BYO key makes its provider's whole
    catalog eligible and takes precedence; on the owner key a model is
    eligible when it is on the user's allowlist and the server holds that
    provider's key; with neither, everything is listed and nothing is
    eligible. Listing the ineligible ones is the point: the page shows what
    exists and what would unlock it instead of a list that silently shrank.
    """
    ent = budget.get_entitlement(user)
    creds = user_settings.credentials(user.id)
    providers = []
    owner_allowed: list = []
    if creds.has_key:
        provider = creds.ai_provider
        providers.append(_provider_entry(provider, _catalog(provider)))
    else:
        # Every provider the fleet models, not a hardcoded pair. This said
        # ("openai", "anthropic") from before xAI and DeepSeek existed, so two
        # fully modelled and fully priced providers were invisible here while
        # being selectable everywhere else. owner_allowed_models already
        # intersects with the keys the server actually holds.
        if ent.owner_key:
            owner_allowed = budget.owner_allowed_models(user.groups)
        for provider in ai.MODEL_CATALOG:
            keyed = bool(routing.server_key(provider))
            models_list = []
            for m in _catalog(provider):
                if m["model"] in owner_allowed:
                    models_list.append(m)
                elif ent.owner_key and keyed:
                    models_list.append({**m, "eligible": False, "reason": NOT_ON_ALLOWLIST})
                else:
                    models_list.append({**m, "eligible": False, "reason": NEEDS_OWN_KEY})
            if models_list:
                providers.append(_provider_entry(provider, models_list))
    return {
        "providers": providers,
        "owner_key_models": owner_allowed,
        "key_source": ent.key_source,
        "addable_providers": list(ai.PROVIDERS),
    }
