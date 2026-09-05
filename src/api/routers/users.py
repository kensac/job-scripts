from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import ai, budget, crypto, db, visibility
from api.auth import AuthedUser, require_service, require_user
from api.models import ApiKeyPut, Criteria, SettingsPut
from core import providers as core_providers

router = APIRouter()


def _grants(user: AuthedUser) -> dict:
    ent = budget.get_entitlement(user)
    return {
        "owner_key": ent.owner_key,
        "weekly_token_budget": ent.weekly_token_budget,
        "spent_this_week": ent.spent_this_week,
        "has_byo_key": ent.has_byo_key,
        "key_source": ent.key_source,
        "owner_key_models": budget.owner_allowed_models(user.groups) if ent.owner_key else [],
    }


@router.post("/users/bootstrap")
def bootstrap(user: AuthedUser = Depends(require_user)):
    db.execute(
        "INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
        (user.id,),
    )
    return {
        "user": {"id": user.id, "sub": user.sub, "email": user.email, "name": user.name},
        "grants": _grants(user),
    }


@router.get("/user/usage")
def usage(user: AuthedUser = Depends(require_user)):
    """Grants, plus the spend history the admin view already computed.

    These are the same two aggregates /admin/users/{id} runs, scoped to the
    caller. They existed for months and only an admin could see them, which is
    why a user's own Usage page had nothing to show.
    """
    return {
        **_grants(user),
        "spend_by_day": db.query(
            """
            SELECT created_at::date AS day, key_source,
                   SUM(total_tokens) AS tokens, COUNT(*) AS calls,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
            FROM api_usage WHERE user_id = %s AND created_at > now() - interval '30 days'
            GROUP BY 1, 2 ORDER BY 1
            """,
            (user.id,),
        ),
        "spend_by_purpose": db.query(
            """
            SELECT purpose, model, SUM(total_tokens) AS tokens, COUNT(*) AS calls,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                   COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
            FROM api_usage WHERE user_id = %s GROUP BY 1, 2 ORDER BY 3 DESC
            """,
            (user.id,),
        ),
    }


# Derived from the datasheets rather than listed here. This was a hardcoded
# map of three providers and it never gained xai or deepseek, so adding them to
# the picker would have raised KeyError on a page that only ever showed two.
# The reasoning parameter is per MODEL, not per provider - xAI accepts it on
# some of its models and rejects it on others - so the provider offers the
# union and validate_params still checks the specific model.
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
    settings = db.query_one(
        "SELECT ai_provider, api_key_enc IS NOT NULL AS has_key "
        "FROM user_settings WHERE user_id = %s",
        (user.id,),
    )
    providers = []
    owner_allowed: list = []
    if settings and settings["has_key"]:
        provider = settings["ai_provider"] or "openai"
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
            keyed = bool(ai.server_key(provider))
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


def _effective_model(user: AuthedUser) -> dict:
    """What will actually run, beside what was chosen.

    The stored ai_model is not the model on shared credits: one outside the
    user's allowlist is replaced at call time. Reading the setting back showed
    the choice and never the substitution, so the screen agreed with the
    database and both disagreed with what ran.

    Resolved through the same call the work uses rather than re-deriving the
    rule here - a second copy would drift and start describing a substitution
    that does not happen.
    """
    try:
        cfg = budget.resolve_ai_config(user.id, budget.get_entitlement(user))
    except (LookupError, PermissionError) as exc:
        # No model, or out of budget. Both are real answers about what will
        # run - nothing - and neither is a substitution.
        return {"effective_model": None, "unavailable_reason": type(exc).__name__}
    return {
        "effective_model": cfg.model,
        "substituted_from": cfg.substituted_from,
        "substitution_reason": cfg.substitution_reason,
        "unavailable_reason": None,
    }


@router.get("/user/settings")
def get_settings(user: AuthedUser = Depends(require_user)):
    row = db.query_one(
        "SELECT column_layout, prefs, ai_provider, ai_base_url, ai_model, ai_params, "
        "bypass_sponsorship_filter, criteria, email_digest, "
        "api_key_enc IS NOT NULL AS has_byo_key "
        "FROM user_settings WHERE user_id = %s",
        (user.id,),
    )
    settings = {**(row or _SETTINGS_DEFAULTS)}
    # Criteria in their full shape, defaults filled, whatever the row holds:
    # a client that reads support for a criterion by the key's presence
    # must not depend on what this user happened to save before the key
    # existed. A stale key the model no longer knows drops out here too.
    settings["criteria"] = Criteria.model_validate(settings.get("criteria") or {}).model_dump(
        mode="json"
    )
    return {**settings, **_effective_model(user)}


_SETTINGS_DEFAULTS = {
    "column_layout": None,
    "prefs": {},
    "ai_provider": "openai",
    "ai_base_url": None,
    "ai_model": None,
    "ai_params": {},
    "bypass_sponsorship_filter": True,
    "criteria": {},
    "email_digest": False,
    "has_byo_key": False,
}


@router.put("/user/settings")
def put_settings(body: SettingsPut, user: AuthedUser = Depends(require_user)):
    row = None
    if body.ai_params is not None or body.ai_model is not None:
        row = db.query_one(
            "SELECT ai_provider, api_key_enc IS NOT NULL AS has_key "
            "FROM user_settings WHERE user_id = %s",
            (user.id,),
        )
    provider = (row or {}).get("ai_provider") or "openai"
    if body.ai_params is not None:
        error = ai.validate_params(provider, body.ai_params, body.ai_model)
        if error:
            raise HTTPException(400, detail={"code": "INVALID_PARAMS", "message": error})
    if body.ai_model is not None:
        has_key = bool(row and row["has_key"])
        if has_key:
            catalog = {m["model"] for m in ai.MODEL_CATALOG[provider]}
            valid = provider == "openai_compatible" or body.ai_model in catalog
        else:
            ent = budget.get_entitlement(user)
            valid = ent.owner_key and body.ai_model in budget.owner_allowed_models(user.groups)
        if not valid:
            raise HTTPException(
                400,
                detail={
                    "code": "INVALID_MODEL",
                    "message": "that model is not available with your current key",
                },
            )
    db.execute(
        """
        INSERT INTO user_settings (user_id, column_layout, prefs, ai_model, ai_params,
                                   bypass_sponsorship_filter, criteria,
                                   email_digest, updated_at)
        VALUES (%(uid)s, %(layout)s, COALESCE(%(prefs)s, '{}'::jsonb),
                %(model)s, COALESCE(%(params)s, '{}'::jsonb),
                COALESCE(%(bypass)s, TRUE), COALESCE(%(criteria)s, '{}'::jsonb),
                COALESCE(%(digest)s, FALSE), now())
        ON CONFLICT (user_id) DO UPDATE SET
            column_layout = COALESCE(EXCLUDED.column_layout, user_settings.column_layout),
            prefs = COALESCE(%(prefs)s, user_settings.prefs),
            ai_model = COALESCE(%(model)s, user_settings.ai_model),
            ai_params = COALESCE(%(params)s, user_settings.ai_params),
            bypass_sponsorship_filter = COALESCE(%(bypass)s, user_settings.bypass_sponsorship_filter),
            criteria = COALESCE(%(criteria)s, user_settings.criteria),
            email_digest = COALESCE(%(digest)s, user_settings.email_digest),
            updated_at = now()
        """,
        {
            "uid": user.id,
            "layout": db.jsonb(body.column_layout) if body.column_layout is not None else None,
            "prefs": db.jsonb(body.prefs) if body.prefs is not None else None,
            "model": body.ai_model,
            "params": db.jsonb(body.ai_params) if body.ai_params is not None else None,
            "bypass": body.bypass_sponsorship_filter,
            "criteria": db.jsonb(body.criteria.model_dump(mode="json"))
            if body.criteria is not None
            else None,
            "digest": body.email_digest,
        },
    )
    if body.email_digest:
        import secrets as _secrets

        db.execute(
            "UPDATE user_settings SET digest_token = %s "
            "WHERE user_id = %s AND digest_token IS NULL",
            (_secrets.token_urlsafe(24), user.id),
        )
    # The saved settings, in the shape GET serves them, so a client can read
    # what the write did from the write. The deployed page read criteria off
    # this response to decide whether a criterion was supported, found no
    # criteria in {"ok": true}, and told Kanishk his include list was
    # unsupported by an API that had just stored it.
    visibility.request_refresh(user.id)
    return {"ok": True, **get_settings(user)}


@router.get("/digest/unsubscribe")
@router.post("/digest/unsubscribe")
def digest_unsubscribe(token: str, _: None = Depends(require_service)):
    """Unsubscribe from digest emails, identified purely by the emailed token,
    no user session required. POST is the write the page should make: the
    emailed link is a GET that mail clients and link scanners prefetch, and
    a page that wrote on render would unsubscribe people who never clicked.
    The mail's List-Unsubscribe-Post header already makes one-click clients
    POST. GET stays only until the page has moved its write behind POST."""
    row = db.query_one(
        "UPDATE user_settings SET email_digest = FALSE, updated_at = now() "
        "WHERE digest_token = %s RETURNING user_id",
        (token,),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown token"})
    return {"ok": True}


@router.put("/user/settings/api-key")
def put_api_key(body: ApiKeyPut, user: AuthedUser = Depends(require_user)):
    if body.provider not in ai.PROVIDERS:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_PROVIDER",
                "message": f"provider must be one of {ai.PROVIDERS}",
            },
        )
    if body.provider == "openai_compatible":
        if not body.base_url:
            raise HTTPException(
                400,
                detail={
                    "code": "BASE_URL_REQUIRED",
                    "message": "openai_compatible needs a base_url",
                },
            )
        from api import ssrf

        error = ssrf.validate_base_url(body.base_url)
        if error is None:
            try:
                ssrf.resolve_public_ip(urlparse(body.base_url).hostname or "")
            except ValueError as exc:
                error = str(exc)
        if error:
            raise HTTPException(400, detail={"code": "INVALID_BASE_URL", "message": error})
    db.execute(
        """
        INSERT INTO user_settings (user_id, api_key_enc, ai_provider, ai_base_url, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (user_id) DO UPDATE SET
            api_key_enc = EXCLUDED.api_key_enc,
            ai_provider = EXCLUDED.ai_provider,
            ai_base_url = EXCLUDED.ai_base_url,
            ai_model = NULL,
            updated_at = now()
        """,
        (user.id, crypto.encrypt(body.api_key), body.provider, body.base_url),
    )
    return {"ok": True}


@router.delete("/user/settings/api-key")
def delete_api_key(user: AuthedUser = Depends(require_user)):
    db.execute(
        "UPDATE user_settings SET api_key_enc = NULL, ai_base_url = NULL, "
        "ai_provider = 'openai', ai_model = NULL, updated_at = now() WHERE user_id = %s",
        (user.id,),
    )
    return {"ok": True}
