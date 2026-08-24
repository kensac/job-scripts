from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from api import crypto, db
from api.auth import AuthedUser


@dataclass
class Entitlement:
    owner_key: bool
    weekly_token_budget: Optional[int]
    spent_this_week: int
    has_byo_key: bool

    @property
    def key_source(self) -> Optional[str]:
        if self.has_byo_key:
            return "byo"
        if self.owner_key and (
            self.weekly_token_budget is None
            or self.spent_this_week < self.weekly_token_budget
        ):
            return "owner"
        return None


def _owner_budget(groups: List[str]) -> tuple[bool, Optional[int]]:
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
    )


def resolve_ai_config(user_id: int, entitlement: Entitlement):
    """Returns an ai.AIConfig or raises LookupError / PermissionError."""
    import os

    from api import ai

    settings = db.query_one(
        "SELECT api_key_enc, ai_provider, ai_base_url, ai_model, ai_params "
        "FROM user_settings WHERE user_id = %s",
        (user_id,),
    ) or {}
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
        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            model = settings.get("ai_model") or ai.DEFAULT_MODELS["openai"]
            if model not in ai.OWNER_KEY_MODELS:
                model = ai.DEFAULT_MODELS["openai"]
            return ai.AIConfig(
                provider="openai",
                api_key=key,
                key_source="owner",
                model=model,
                params={k: v for k, v in params.items() if k != "temperature"},
            )
    raise LookupError("NO_API_KEY")


def record_usage(
    user_id: int,
    key_source: str,
    purpose: str,
    model: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> None:
    db.execute(
        "INSERT INTO api_usage (user_id, key_source, purpose, model, "
        "prompt_tokens, completion_tokens, total_tokens) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (user_id, key_source, purpose, model, prompt_tokens, completion_tokens, total_tokens),
    )
    from api import metrics

    metrics.AI_TOKENS.labels(key_source, purpose).inc(total_tokens)
