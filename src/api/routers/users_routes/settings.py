from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import ai, budget, crypto, db, user_settings
from api.auth import AuthedUser, is_admin, require_service, require_user
from api.board import visibility
from api.models import ApiKeyPut, Criteria, Ok, SettingsPut
from api.problem import refuse
from core.answers import DEFAULT_STYLE

router = APIRouter()


class EffectiveModel(BaseModel):
    """What will actually run, and why it is not what was chosen.

    Every field is always present. The dict this replaced omitted
    `substituted_from` and `substitution_reason` when there was no model to
    run at all, so a client had to tell an absent key from a null one; both
    said the same thing, which is that nothing was substituted.
    """

    effective_model: str | None
    substituted_from: str | None = None
    substitution_reason: str | None = None
    unavailable_reason: str | None
    unavailable_code: str | None
    unavailable_message: str | None


class UserSettings(EffectiveModel):
    """A person's saved settings, plus the two read-only fields the settings
    page needs beside them: the built-in writing style it uses as a
    placeholder, and what will actually run."""

    # The board grid's own shape, which this API stores and never reads, so
    # declaring its keys here would be this module claiming to know a page's
    # vocabulary. Null when neither the person nor the configured default has
    # one.
    column_layout: Any
    prefs: dict[str, Any]
    ai_provider: str
    ai_base_url: str | None
    ai_model: str | None
    ai_params: dict[str, Any]
    bypass_sponsorship_filter: bool
    criteria: Criteria
    email_digest: bool
    writing_style: str | None
    has_byo_key: bool
    default_style: str


class UserSettingsSaved(UserSettings):
    """The write's answer: the settings as GET serves them, and that it took.

    The deployed page read criteria off this response to decide whether a
    criterion was supported, found no criteria in {"ok": true}, and told
    Kanishk his include list was unsupported by an API that had just stored
    it."""

    ok: bool = True


def _effective_model(user: AuthedUser) -> EffectiveModel:
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
    except budget.AIAccessError as exc:
        # No model, or out of budget. Both are real answers about what will
        # run - nothing - and neither is a substitution.
        return EffectiveModel(
            effective_model=None,
            unavailable_reason="PermissionError"
            if isinstance(exc, PermissionError)
            else "LookupError",
            unavailable_code=exc.reason,
            unavailable_message=exc.message,
        )
    return EffectiveModel(
        effective_model=cfg.model,
        substituted_from=cfg.substituted_from,
        substitution_reason=cfg.substitution_reason,
        unavailable_reason=None,
        unavailable_code=None,
        unavailable_message=None,
    )


@router.get("/user/settings")
def get_settings(user: AuthedUser = Depends(require_user)) -> UserSettings:
    settings = user_settings.served(user.id)
    # New accounts and resets use the same configured board layout.
    if settings.get("column_layout") is None:
        settings["column_layout"] = db.get_config("board_default_column_layout") or None
    # Criteria in their full shape, defaults filled, whatever the row holds:
    # a client that reads support for a criterion by the key's presence
    # must not depend on what this user happened to save before the key
    # existed. A stale key the model no longer knows drops out here too.
    settings["criteria"] = Criteria.model_validate(settings.get("criteria") or {})
    # Read-only, beside the field it is the placeholder for: the settings
    # page is where a person writes their style, so it shows the default
    # there without reading a job's application view to find it.
    return UserSettings(
        **settings, default_style=DEFAULT_STYLE, **_effective_model(user).model_dump()
    )


# A person who is not an admin keeps postings at most this many days old:
# 30 when they set nothing, never more (Kanishk, 2026-09-08). Checked where
# the criteria are written; the same groups gate the admin routes.
MAX_AGE_CAP_DAYS = 30


@router.put("/user/settings")
def put_settings(body: SettingsPut, user: AuthedUser = Depends(require_user)) -> UserSettingsSaved:
    if body.criteria is not None and not is_admin(user.groups):
        if (body.criteria.max_age_days or 0) > MAX_AGE_CAP_DAYS:
            raise refuse(
                400,
                "MAX_AGE_DAYS",
                f"Postings older than {MAX_AGE_CAP_DAYS} days are not kept on your "
                f"board; {MAX_AGE_CAP_DAYS} is the most your account can set.",
            )
        if body.criteria.max_age_days is None:
            body.criteria.max_age_days = MAX_AGE_CAP_DAYS
    creds = user_settings.credentials(user.id)
    provider = creds.ai_provider
    if body.ai_params is not None:
        error = ai.validate_params(provider, body.ai_params, body.ai_model)
        if error:
            raise refuse(400, "INVALID_PARAMS", error)
    if body.ai_model is not None:
        if creds.has_key:
            catalog = {m["model"] for m in ai.MODEL_CATALOG[provider]}
            valid = provider == "openai_compatible" or body.ai_model in catalog
        else:
            ent = budget.get_entitlement(user)
            valid = ent.owner_key and body.ai_model in budget.owner_allowed_models(user.groups)
        if not valid:
            raise refuse(400, "INVALID_MODEL", "that model is not available with your current key")
    sent = body.model_fields_set
    user_settings.save(
        user.id,
        user_settings.Update(
            column_layout=body.column_layout,
            column_layout_set="column_layout" in sent,
            prefs=body.prefs,
            ai_model=body.ai_model,
            ai_model_set="ai_model" in sent,
            ai_params=body.ai_params,
            bypass_sponsorship_filter=body.bypass_sponsorship_filter,
            criteria=body.criteria.model_dump(mode="json") if body.criteria is not None else None,
            email_digest=body.email_digest,
            writing_style=body.writing_style,
            writing_style_set="writing_style" in sent,
        ),
    )
    # The saved settings, in the shape GET serves them, so a client can read
    # what the write did from the write.
    visibility.request_refresh(user.id)
    return UserSettingsSaved(**get_settings(user).model_dump())


@router.get("/digest/unsubscribe")
@router.post("/digest/unsubscribe")
def digest_unsubscribe(token: str, _: None = Depends(require_service)) -> Ok:
    """Unsubscribe from digest emails, identified purely by the emailed token,
    no user session required. POST is the write the page should make: the
    emailed link is a GET that mail clients and link scanners prefetch, and
    a page that wrote on render would unsubscribe people who never clicked.
    The mail's List-Unsubscribe-Post header already makes one-click clients
    POST. GET stays only until the page has moved its write behind POST."""
    if not user_settings.unsubscribe_digest(token):
        raise refuse(404, "NOT_FOUND", "unknown token")
    return Ok()


@router.put("/user/settings/api-key")
def put_api_key(body: ApiKeyPut, user: AuthedUser = Depends(require_user)) -> Ok:
    if body.provider not in ai.PROVIDERS:
        raise refuse(400, "INVALID_PROVIDER", f"provider must be one of {ai.PROVIDERS}")
    if body.provider == "openai_compatible":
        if not body.base_url:
            raise refuse(400, "BASE_URL_REQUIRED", "openai_compatible needs a base_url")
        from api import ssrf

        error = ssrf.validate_base_url(body.base_url)
        if error is None:
            try:
                ssrf.resolve_public_ip(urlparse(body.base_url).hostname or "")
            except ValueError as exc:
                error = str(exc)
        if error:
            raise refuse(400, "INVALID_BASE_URL", error)
    user_settings.save_api_key(user.id, crypto.encrypt(body.api_key), body.provider, body.base_url)
    return Ok()


@router.delete("/user/settings/api-key")
def delete_api_key(user: AuthedUser = Depends(require_user)) -> Ok:
    user_settings.clear_api_key(user.id)
    return Ok()
