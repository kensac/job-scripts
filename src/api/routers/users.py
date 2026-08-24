from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import ai, budget, crypto, db
from api.auth import AuthedUser, require_user
from api.models import ApiKeyPut, SettingsPut

router = APIRouter()


def _grants(user: AuthedUser) -> dict:
    ent = budget.get_entitlement(user)
    return {
        "owner_key": ent.owner_key,
        "weekly_token_budget": ent.weekly_token_budget,
        "spent_this_week": ent.spent_this_week,
        "has_byo_key": ent.has_byo_key,
        "key_source": ent.key_source,
        "owner_key_models": ai.owner_models(ent.weekly_token_budget is None)
        if ent.owner_key
        else [],
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
    return _grants(user)


_PROVIDER_PARAMS = {
    "openai": ["reasoning_effort", "max_output_tokens"],
    "anthropic": ["effort", "max_output_tokens"],
    "openai_compatible": ["temperature", "max_output_tokens"],
}


class ModelEntry(BaseModel):
    model: str
    note: str


class ProviderEntry(BaseModel):
    provider: str
    default_model: Optional[str]
    models: List[ModelEntry]
    params: List[str]


class ModelsResponse(BaseModel):
    providers: List[ProviderEntry]
    owner_key_models: List[str]
    key_source: Optional[str]
    addable_providers: List[str]


def _provider_entry(provider: str, models: list) -> dict:
    return {
        "provider": provider,
        "default_model": ai.DEFAULT_MODELS[provider],
        "models": models,
        "params": _PROVIDER_PARAMS[provider],
    }


@router.get("/models", response_model=ModelsResponse)
def models(user: AuthedUser = Depends(require_user)):
    """Only the options valid for this user right now: their BYO provider's
    catalog if they have a key (it takes precedence), else the owner-key
    allowlist if granted, else nothing runnable."""
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
        providers.append(_provider_entry(provider, ai.MODEL_CATALOG[provider]))
    elif ent.owner_key:
        owner_allowed = ai.owner_models(ent.weekly_token_budget is None)
        for provider in ("openai", "anthropic"):
            models_list = [
                m for m in ai.MODEL_CATALOG[provider] if m["model"] in owner_allowed
            ]
            if models_list:
                providers.append(_provider_entry(provider, models_list))
    return {
        "providers": providers,
        "owner_key_models": owner_allowed,
        "key_source": ent.key_source,
        "addable_providers": list(ai.PROVIDERS),
    }


@router.get("/user/settings")
def get_settings(user: AuthedUser = Depends(require_user)):
    row = db.query_one(
        "SELECT column_layout, prefs, ai_provider, ai_base_url, ai_model, ai_params, "
        "bypass_sponsorship_filter, api_key_enc IS NOT NULL AS has_byo_key "
        "FROM user_settings WHERE user_id = %s",
        (user.id,),
    )
    return row or {
        "column_layout": None,
        "prefs": {},
        "ai_provider": "openai",
        "ai_base_url": None,
        "ai_model": None,
        "ai_params": {},
        "bypass_sponsorship_filter": True,
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
        error = ai.validate_params(provider, body.ai_params)
        if error:
            raise HTTPException(400, detail={"code": "INVALID_PARAMS", "message": error})
    if body.ai_model is not None:
        has_key = bool(row and row["has_key"])
        if has_key:
            catalog = {m["model"] for m in ai.MODEL_CATALOG[provider]}
            valid = provider == "openai_compatible" or body.ai_model in catalog
        else:
            ent = budget.get_entitlement(user)
            valid = ent.owner_key and body.ai_model in ai.owner_models(
                ent.weekly_token_budget is None
            )
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
                                   bypass_sponsorship_filter, updated_at)
        VALUES (%(uid)s, %(layout)s, COALESCE(%(prefs)s, '{}'::jsonb),
                %(model)s, COALESCE(%(params)s, '{}'::jsonb),
                COALESCE(%(bypass)s, FALSE), now())
        ON CONFLICT (user_id) DO UPDATE SET
            column_layout = COALESCE(EXCLUDED.column_layout, user_settings.column_layout),
            prefs = COALESCE(%(prefs)s, user_settings.prefs),
            ai_model = COALESCE(%(model)s, user_settings.ai_model),
            ai_params = COALESCE(%(params)s, user_settings.ai_params),
            bypass_sponsorship_filter = COALESCE(%(bypass)s, user_settings.bypass_sponsorship_filter),
            updated_at = now()
        """,
        {
            "uid": user.id,
            "layout": db.jsonb(body.column_layout) if body.column_layout is not None else None,
            "prefs": db.jsonb(body.prefs) if body.prefs is not None else None,
            "model": body.ai_model,
            "params": db.jsonb(body.ai_params) if body.ai_params is not None else None,
            "bypass": body.bypass_sponsorship_filter,
        },
    )
    return {"ok": True}


@router.put("/user/settings/api-key")
def put_api_key(body: ApiKeyPut, user: AuthedUser = Depends(require_user)):
    if body.provider not in ai.PROVIDERS:
        raise HTTPException(
            400,
            detail={"code": "INVALID_PROVIDER", "message": f"provider must be one of {ai.PROVIDERS}"},
        )
    if body.provider == "openai_compatible":
        if not body.base_url:
            raise HTTPException(
                400,
                detail={"code": "BASE_URL_REQUIRED", "message": "openai_compatible needs a base_url"},
            )
        from api import ssrf

        error = ssrf.validate_base_url(body.base_url)
        if error is None:
            try:
                ssrf.resolve_public_ip(urlparse(body.base_url).hostname or "")
            except ValueError as exc:
                error = str(exc)
        if error:
            raise HTTPException(
                400, detail={"code": "INVALID_BASE_URL", "message": error}
            )
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
