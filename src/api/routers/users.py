from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import ai, budget, crypto, db
from api.auth import AuthedUser, require_service, require_user
from api.models import ApiKeyPut, SettingsPut
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
        providers.append(_provider_entry(provider, _catalog(provider)))
    elif ent.owner_key:
        owner_allowed = budget.owner_allowed_models(user.groups)
        # Every provider the fleet models, not a hardcoded pair. This said
        # ("openai", "anthropic") from before xAI and DeepSeek existed, so two
        # fully modelled and fully priced providers were invisible here while
        # being selectable everywhere else. owner_allowed_models already
        # intersects with the keys the server actually holds, so listing them
        # all cannot offer something unrunnable.
        for provider in ai.MODEL_CATALOG:
            models_list = [m for m in _catalog(provider) if m["model"] in owner_allowed]
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
    return {**(row or _SETTINGS_DEFAULTS), **_effective_model(user)}


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
    return {"ok": True}


@router.get("/digest/unsubscribe")
def digest_unsubscribe(token: str, _: None = Depends(require_service)):
    """One-click unsubscribe target from digest emails; identified purely by
    the emailed token, no user session required."""
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


class IdentitiesPut(BaseModel):
    addresses: list[str]


def _normalize_addresses(raw: list[str]) -> list[str]:
    """Canonical form is the server's, so the UI can render the echo rather
    than its own input and the two cannot drift."""
    seen: dict[str, None] = {}
    for value in raw:
        address = (value or "").strip().lower()
        if address:
            seen.setdefault(address, None)
    return sorted(seen)


def _identities_payload(user_id: int, proposed: list[str] | None = None) -> dict:
    from api.tasks.mail_classify import count_would_heal, identities_for
    from core.identity import MIN_CANDIDATE_SHARE, MIN_MAILBOX_MESSAGES

    total_row = db.query_one(
        "SELECT COUNT(*) AS n FROM email_messages WHERE user_id = %s", (user_id,)
    )
    total = (total_row or {}).get("n") or 0
    counts = db.query(
        """
        WITH addrs AS (
            SELECT lower(a) AS addr, m.id FROM email_messages m,
                 LATERAL unnest(m.to_emails) a WHERE m.user_id = %(uid)s
            UNION
            SELECT lower(m.from_email), m.id FROM email_messages m
            WHERE m.user_id = %(uid)s AND COALESCE(m.from_email, '') <> ''
        )
        SELECT addr, COUNT(DISTINCT id) AS messages
        FROM addrs WHERE addr LIKE '%%@%%' GROUP BY addr ORDER BY 2 DESC LIMIT 20
        """,
        {"uid": user_id},
    )
    row = (
        db.query_one(
            "SELECT identities, identities_confirmed_at FROM user_settings WHERE user_id = %s",
            (user_id,),
        )
        or {}
    )
    confirmed_at = row.get("identities_confirmed_at")
    derived = set() if confirmed_at else set(identities_for(user_id))
    if confirmed_at:
        from core.identity import AddressCount, derive_identities

        derived = derive_identities([AddressCount(c["addr"], c["messages"]) for c in counts], total)
    # Below the floor the derivation deliberately returns nothing, because a
    # mailbox of forty messages says nothing about who owns it - a greenhouse.io
    # no-reply once got called the owner that way. Saying WHICH case this is
    # lets the page ask the user to type their addresses instead of rendering
    # an empty list as though it were an answer.
    fallback_reason = "mailbox_too_small" if total < MIN_MAILBOX_MESSAGES else None
    return {
        # Evidence beside each address, not just the address: the counts are
        # what make the list self-explanatory. On the real corpus the two true
        # ones sit at 50.6% and 34.0% with a 4.29x drop after them, which is
        # obvious when the shares are visible and invisible when they are not.
        "candidates": [
            {
                "address": c["addr"],
                "messages": c["messages"],
                "share": (c["messages"] / total) if total else None,
                "derived": c["addr"] in derived,
            }
            for c in counts
            if not total or c["messages"] / total >= MIN_CANDIDATE_SHARE
        ],
        "total_messages": total,
        "confirmed": row.get("identities") if confirmed_at else None,
        "confirmed_at": confirmed_at,
        "fallback_reason": fallback_reason,
        # What confirming THIS set would do to mail already booked, so the
        # button can carry the count instead of the response reporting it after
        # the fact. Counted with the predicate the healer itself acts on.
        #
        # Widening is retroactive so this is what it will reach; narrowing is
        # forward-only, so dropping an address contributes nothing here and the
        # UI must not imply a retraction undoes anything.
        "would_reexamine": count_would_heal(
            user_id, proposed if proposed is not None else identities_for(user_id)
        ),
    }


@router.get("/user/identities")
def get_identities(
    proposed: list[str] = Query(default=[]),
    user: AuthedUser = Depends(require_user),
):
    """The addresses this mailbox looks like it belongs to, and what the user
    has said about them.

    `confirmed` is null until the user answers; the step that asks is done iff
    it is not null, derived from this row rather than from a separate flag that
    could disagree with it.

    Pass `proposed` (repeatable) to price a set the user is considering:
    `would_reexamine` then counts the already-booked events confirming it would
    supersede, so the surface can say what the click does before it happens.
    Without it the count describes the set currently in effect, which is what
    the next sweep would do anyway.
    """
    return _identities_payload(user.id, _normalize_addresses(proposed) or None)


@router.put("/user/identities")
def put_identities(body: IdentitiesPut, user: AuthedUser = Depends(require_user)):
    """Record the addresses the user says are theirs.

    WHAT CONFIRMING DOES, because the UI has to tell the user before they click:

    1. PRECEDENCE. The confirmed set replaces the derivation AND the
       users.email fallback outright. Confirm only gmail while logging in with
       psu and psu stops counting as you.

    2. BLAST RADIUS IS NOT FORWARD-ONLY, AND IT IS ASYMMETRIC. ADDING an
       address retroactively corrects mail already booked from it: the next
       classify sweep runs _heal_self_sent, which supersedes events on mail the
       owner sent - the 1,239-event class, including 440 interviews the owner
       scheduled while hiring. REMOVING an address does NOT restore what was
       superseded that way; the healer only ever adds corrections for mail that
       IS currently self-sent, so a retraction leaves the old correction
       standing. Widening is retroactive, narrowing is forward-only.

    3. THE EMPTY SET IS REFUSED. "Nothing is me" would make every message the
       owner sent look like mail from a stranger, which is the exact error this
       feature exists to stop.

    Correction is another PUT. There is no unconfirm: an empty set is the only
    thing that could mean one and it is not a valid claim.
    """
    addresses = _normalize_addresses(body.addresses)
    if not addresses:
        raise HTTPException(
            400,
            "At least one address is required. An empty set would mean nothing in "
            "this mailbox is you, which would misfile every message you sent.",
        )
    db.execute(
        """
        INSERT INTO user_settings (user_id, identities, identities_confirmed_at)
        VALUES (%s, %s, now())
        ON CONFLICT (user_id) DO UPDATE
           SET identities = EXCLUDED.identities,
               identities_confirmed_at = EXCLUDED.identities_confirmed_at
        """,
        (user.id, db.jsonb(addresses)),
    )
    return _identities_payload(user.id)
