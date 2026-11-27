"""Re-running one check on one job by hand, and the models it may run on."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import ai, budget, db, model_calls, user_settings
from api.ai import verdicts
from api.auth import AuthedUser
from api.problem import PROVIDER_REFUSALS, UNAVAILABLE_REFUSALS, refuse
from api.routers.admin.shared import require_admin
from core import providers, routing
from core.answer_inputs import LIVE_CHECK_INPUT_CHARS
from core.answers import FilterVerdict
from core.checks import POSTING_CHECKS
from core.filters import build_custom_instructions

router = APIRouter()


@dataclass(frozen=True)
class _ResolvedCheck:
    check_type: str
    instructions: str
    response_model: type[BaseModel]
    verdict_of: Callable[[Any], tuple[bool, str]]
    filter_name: str | None = None
    prompt_hash: str | None = None


def _recheck_models(user: AuthedUser) -> list[str]:
    """The models a manual re-check may run: what this caller is allowed on
    the server key, intersected with the keys the server holds. Same rule as
    everything else that spends on the owner key; not a second list."""
    return budget.owner_allowed_models(user.groups)


def _recheck_defaults(user_id: int) -> dict[str, str]:
    """Per check option, the model this person last chose for a re-check.
    Lives in the person's prefs so it needs no schema and travels with the
    rest of their preferences."""
    defaults = user_settings.prefs(user_id).get("recheck_models") or {}
    return {k: v for k, v in defaults.items() if isinstance(v, str)}


def _remember_recheck_model(user_id: int, check: str, model: str) -> None:
    user_settings.merge_pref(user_id, "recheck_models", check, model)


def _lookup_custom_filter(addressing: str, ref: str) -> dict[str, Any]:
    if addressing == "hash":
        # Verdicts are cached by prompt_hash, not by filter id. Several
        # users' filters can share one hash. Any of them reproduces the
        # same check, so re-running by hash is the honest admin-side
        # addressing for a verdict row.
        flt = db.query_one(
            "SELECT user_id, name, prompt, on_ambiguous, prompt_hash FROM user_filters "
            "WHERE prompt_hash = %s ORDER BY id LIMIT 1",
            (ref,),
        )
        if not flt:
            # A managed board's question is cached by its prompt hash too, and
            # its verdicts are named for the board (managed_boards).
            flt = db.query_one(
                "SELECT NULL AS user_id, 'managed-board:' || id AS name, prompt, on_ambiguous, "
                "prompt_hash FROM managed_boards WHERE prompt_hash = %s ORDER BY id LIMIT 1",
                (ref,),
            )
    else:
        flt = db.query_one(
            "SELECT user_id, name, prompt, on_ambiguous, prompt_hash FROM user_filters WHERE id = %s",
            (int(ref),),
        )
    if not flt:
        raise refuse(404, "NOT_FOUND", "unknown filter")
    return flt


def _resolve_check(check: str, with_reason: bool) -> _ResolvedCheck:
    spec = POSTING_CHECKS.get(check)
    if spec:
        # with_reason picks the cheaper schema where the check has one: the
        # sweep settles thousands at a time and the reason is read when a
        # person opens one.
        return _ResolvedCheck(
            check_type=check,
            instructions=spec.instructions,
            response_model=spec.model_for(with_reason),
            verdict_of=spec.verdict_of,
        )
    addressing, _, ref = check.partition(":")
    if addressing == "hash" or (addressing == "filter" and ref.isdecimal()):
        flt = _lookup_custom_filter(addressing, ref)
        filter_name = (
            flt["name"] if flt["user_id"] is None else f"user{flt['user_id']}:{flt['name']}"
        )
        return _ResolvedCheck(
            check_type="custom",
            instructions=build_custom_instructions(flt["prompt"], flt["on_ambiguous"]),
            response_model=FilterVerdict,
            verdict_of=lambda p: (p.should_filter, p.reason),
            filter_name=filter_name,
            prompt_hash=flt["prompt_hash"],
        )
    raise refuse(
        400,
        "INVALID_CHECK",
        f"check must be one of {', '.join(POSTING_CHECKS)}, filter:<id>, or hash:<prompt_hash>",
    )


def _build_recheck_config(user: AuthedUser, body: RunCheckBody) -> ai.AIConfig:
    allowed = _recheck_models(user)
    if body.model is not None:
        if body.model not in allowed:
            raise refuse(
                400,
                "MODEL_NOT_ALLOWED",
                f"{body.model} is not a model you can run a re-check on",
                allowed=allowed,
            )
        model = body.model
    else:
        # The model this person ended on for this option last time, if it is
        # still one they may run; otherwise the cheapest default.
        remembered = _recheck_defaults(user.id).get(body.check)
        model = remembered if remembered in allowed else ai.DEFAULT_OPENAI_MODEL
    provider = providers.provider_of(model) or "openai"
    key = routing.server_key(provider)
    if not key:
        raise refuse(503, "NO_SERVER_KEY", "no server key")
    return ai.AIConfig(
        provider=provider,
        api_key=key,
        key_source="owner",
        model=model,
        params={"reasoning_effort": "medium" if body.with_reason else "low"},
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
    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (body.job_id,))
    if not job:
        raise refuse(404, "NOT_FOUND", "unknown job")
    target = _resolve_check(body.check, body.with_reason)
    # Re-fetch: a recheck against cached text cannot discover that a posting
    # has since closed, which is usually the whole reason for asking.
    fresh, closure_signal = await verdicts.refresh_page(
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
        raise refuse(409, "NO_CONTENT", "could not fetch this posting just now")
    cfg = _build_recheck_config(user, body)

    # The re-check runs on the server's key for the admin who asked, and
    # until the call ledger it was booked nowhere but its verdict row.
    parsed, usage = await verdicts.run_check(
        cfg,
        url=job["url"],
        check_type=target.check_type,
        instructions=target.instructions,
        input_text=fresh.text[:LIVE_CHECK_INPUT_CHARS],
        response_model=target.response_model,
        verdict_of=target.verdict_of,
        booking=budget.Booking(model_calls.Payer(user_id=user.id), "owner", "manual"),
        page_fetch_id=fresh.fetch_id,
        company=job["company"],
        job_title=job["title"],
        filter_name=target.filter_name,
        prompt_hash=target.prompt_hash,
        context="manual",
    )
    if parsed is None:
        raise refuse(502, "NO_VERDICT", "the model returned no usable answer; try again")
    rejected, reason = target.verdict_of(parsed)
    if body.model is not None:
        # Only a run that happened sets the default: a refused or failed
        # choice is not the one the person "last used".
        _remember_recheck_model(user.id, body.check, cfg.model)
    return CheckRerun(
        check=body.check,
        status="rejected" if rejected else "passed",
        reason=reason,
        tokens=usage.get("total_tokens", 0) or 0,
        model=cfg.model,
    )
