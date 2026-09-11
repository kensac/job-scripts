"""Re-running one check on one job by hand, and the models it may run on."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import ai, db
from api.auth import AuthedUser
from api.problem import PROVIDER_REFUSALS, UNAVAILABLE_REFUSALS
from api.routers.admin.shared import require_admin

router = APIRouter()


def _recheck_models(user: AuthedUser) -> list[str]:
    """The models a manual re-check may run: what this caller is allowed on
    the server key, intersected with the keys the server holds. Same rule as
    everything else that spends on the owner key; not a second list."""
    from api import budget

    return budget.owner_allowed_models(user.groups)


def _recheck_defaults(user_id: int) -> dict[str, str]:
    """Per check option, the model this person last chose for a re-check.
    Lives in user_settings.prefs so it needs no schema and travels with the
    rest of their preferences."""
    row = db.query_one("SELECT prefs FROM user_settings WHERE user_id = %s", (user_id,))
    prefs = (row or {}).get("prefs") or {}
    defaults = prefs.get("recheck_models") or {}
    return {k: v for k, v in defaults.items() if isinstance(v, str)}


def _remember_recheck_model(user_id: int, check: str, model: str) -> None:
    db.execute(
        """
        INSERT INTO user_settings (user_id, prefs)
        VALUES (%(uid)s, jsonb_build_object('recheck_models', jsonb_build_object(%(check)s::text, %(model)s::text)))
        ON CONFLICT (user_id) DO UPDATE SET prefs =
            COALESCE(user_settings.prefs, '{}'::jsonb)
            || jsonb_build_object('recheck_models',
                 COALESCE(user_settings.prefs->'recheck_models', '{}'::jsonb)
                 || jsonb_build_object(%(check)s::text, %(model)s::text)),
            updated_at = now()
        """,
        {"uid": user_id, "check": check, "model": model},
    )


class RecheckOptions(BaseModel):
    """`defaults` is keyed by check option, not by check type: a re-check may
    be asked for by filter id or prompt hash, and the person's last choice is
    remembered against what they asked for."""

    models: list[str]
    defaults: dict[str, str]


@router.get("/checks/options")
def recheck_options(user: AuthedUser = Depends(require_admin)) -> RecheckOptions:
    """What a re-check may run on, and what this person chose last time per
    option, so the picker opens on the model they ended on rather than the
    cheapest one every time."""
    return RecheckOptions(models=_recheck_models(user), defaults=_recheck_defaults(user.id))


class CheckRerun(BaseModel):
    """The verdict the re-run produced, and what produced it. `check` echoes
    what was asked for, which is not always `check_type`: filter:<id> and
    hash:<prompt_hash> both run as a custom check."""

    check: str
    status: str
    reason: str
    tokens: int
    model: str


class PostingGone(BaseModel):
    """No re-run happened, because the re-fetch found the posting closed.

    A separate shape rather than nullable fields on CheckRerun: nothing was
    asked of a model here, so `model` and a token count would be a guess, and
    `closure_signal` names what the fetch saw. `refetched` is always true,
    since a closure can only be discovered by fetching.
    """

    check: str
    status: str
    reason: str | None
    tokens: int
    refetched: bool
    closure_signal: str


class RunCheckBody(BaseModel):
    job_id: int
    check: str
    with_reason: bool = True
    # The model to run on. Omitted: the model last chosen for this check
    # option, else the default. Given: must be one the caller may run, and
    # becomes the default for this option once it has run.
    model: str | None = None


# 503 is this route's own: no other route refuses because the fleet holds no
# server key for the provider the re-check would run on.
@router.post("/checks/run", responses=UNAVAILABLE_REFUSALS | PROVIDER_REFUSALS)
async def run_single_check(
    body: RunCheckBody, user: AuthedUser = Depends(require_admin)
) -> CheckRerun | PostingGone:
    """Manually re-run one check on one job, ignoring the cached verdict. The
    fresh row becomes the latest for that (url, check_type), so visibility
    re-derives from it immediately. No downstream re-run needed, since
    visibility is a read-time predicate rather than stored derived state."""
    from api.ai import verdicts as _verdicts
    from core.answers import FilterVerdict
    from core.checks import POSTING_CHECKS
    from core.filters import build_custom_instructions

    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (body.job_id,))
    if not job:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    filter_name = prompt_hash = None
    check = body.check
    spec = POSTING_CHECKS.get(check)
    if spec:
        instructions = spec.instructions
        # with_reason picks the cheaper schema where the check has one: the
        # sweep settles thousands at a time and the reason is read when a
        # person opens one.
        model_cls, verdict_of = spec.model_for(body.with_reason), spec.verdict_of
    elif check.startswith(("filter:", "hash:")):
        if check.startswith("hash:"):
            # Verdicts are cached by prompt_hash, not by filter id. Several
            # users' filters can share one hash. Any of them reproduces the
            # same check, so re-running by hash is the honest admin-side
            # addressing for a verdict row.
            flt = db.query_one(
                "SELECT user_id, name, prompt, on_ambiguous, prompt_hash FROM user_filters "
                "WHERE prompt_hash = %s ORDER BY id LIMIT 1",
                (check.split(":", 1)[1],),
            )
        else:
            flt = db.query_one(
                "SELECT user_id, name, prompt, on_ambiguous, prompt_hash FROM user_filters WHERE id = %s",
                (int(check.split(":", 1)[1]),),
            )
        if not flt:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
        instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
        model_cls, verdict_of = FilterVerdict, (lambda p: (p.should_filter, p.reason))
        filter_name = f"user{flt['user_id']}:{flt['name']}"
        prompt_hash = flt["prompt_hash"]
        check = "custom"
    else:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_CHECK",
                "message": (
                    f"check must be one of {', '.join(POSTING_CHECKS)}, "
                    "filter:<id>, or hash:<prompt_hash>"
                ),
            },
        )
    # Re-fetch: a recheck against cached text cannot discover that a posting
    # has since closed, which is usually the whole reason for asking.
    fresh, closure_signal = await _verdicts.refresh_content(
        job["url"], company=job["company"], job_title=job["title"], context="manual"
    )
    if fresh is None:
        gone = db.query_one(
            "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
            "ORDER BY id DESC LIMIT 1",
            (job["url"],),
        )
        if closure_signal:
            return PostingGone(
                check=body.check,
                status="rejected",
                reason=(gone or {}).get("reason", ""),
                tokens=0,
                refetched=True,
                closure_signal=closure_signal,
            )
        raise HTTPException(
            409,
            detail={"code": "NO_CONTENT", "message": "could not fetch this posting just now"},
        )
    content = {"input_content": fresh}
    allowed = _recheck_models(user)
    if body.model is not None:
        if body.model not in allowed:
            raise HTTPException(
                400,
                detail={
                    "code": "MODEL_NOT_ALLOWED",
                    "message": f"{body.model} is not a model you can run a re-check on",
                    "allowed": allowed,
                },
            )
        model = body.model
    else:
        # The model this person ended on for this option last time, if it is
        # still one they may run; otherwise the cheapest default.
        remembered = _recheck_defaults(user.id).get(body.check)
        model = remembered if remembered in allowed else ai.DEFAULT_OPENAI_MODEL
    provider = ai.provider_of_model(model) or "openai"
    key = ai.server_key(provider)
    if not key:
        raise HTTPException(503, detail={"code": "NO_SERVER_KEY", "message": "no server key"})
    cfg = ai.AIConfig(
        provider=provider,
        api_key=key,
        key_source="owner",
        model=model,
        params={"reasoning_effort": "medium" if body.with_reason else "low"},
    )
    parsed, usage = await _verdicts.run_check(
        cfg,
        url=job["url"],
        check_type=check,
        instructions=instructions,
        input_text=content["input_content"][:60000],
        response_model=model_cls,
        verdict_of=verdict_of,
        company=job["company"],
        job_title=job["title"],
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        context="manual",
    )
    if parsed is None:
        raise HTTPException(
            502,
            detail={
                "code": "NO_VERDICT",
                "message": "the model returned no usable answer; try again",
            },
        )
    rejected, reason = verdict_of(parsed)
    if body.model is not None:
        # Only a run that happened sets the default: a refused or failed
        # choice is not the one the person "last used".
        _remember_recheck_model(user.id, body.check, model)
    return CheckRerun(
        check=body.check,
        status="rejected" if rejected else "passed",
        reason=reason,
        tokens=usage.get("total_tokens", 0),
        model=model,
    )
