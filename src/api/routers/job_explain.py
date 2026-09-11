"""An on-demand explanation of a posting verdict."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import db
from api.ai import access as ai_access
from api.auth import AuthedUser, require_user
from api.board.access import require_visible_job
from api.problem import AI_REFUSALS

router = APIRouter()

# What a posting check answered. The queries and writers that use one restrict
# to these two, so a third value cannot arrive without their contracts changing.
Verdict = Literal["passed", "rejected"]


class Explained(BaseModel):
    """One check re-run on purpose, with the reasoning the cheap path skips.

    `refetched` and `closure_signal` are present only when the content could
    not be fetched and the fetch itself said why: a 404 or a removal notice is
    an answer about the posting, not a failure to get one."""

    check: str
    status: Verdict
    reason: str | None
    refetched: bool | None = None
    closure_signal: str | None = None


class ExplainBody(BaseModel):
    check: str


@router.post("/user/jobs/{job_id}/explain", responses=AI_REFUSALS)
async def explain_check(
    job_id: int, body: ExplainBody, user: AuthedUser = Depends(require_user)
) -> Explained:
    """On-demand debugging: re-runs one check with the reason-ful schema and
    fuller reasoning (default verdicts skip reasons to save output tokens).
    Records a fresh verdict row (context 'explain') and returns the reason."""
    import dataclasses

    from api import budget
    from api.ai import verdicts as _verdicts
    from core.answers import FilterVerdict
    from core.checks import POSTING_CHECKS
    from core.filters import build_custom_instructions

    # This route writes a verdict into ai_queries, which has no user_id and is
    # resolved latest-row-per-(url, check_type) for EVERY user. An ungated
    # job_id here is therefore not a read leak but a write primitive against
    # everyone's board.
    job = require_visible_job(user, job_id, "j.id, j.url, j.company, j.title")
    fresh, closure_signal = await _verdicts.refresh_content(
        job["url"], company=job["company"], job_title=job["title"], context="explain"
    )
    if fresh is None:
        gone = db.query_one(
            "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
            "ORDER BY id DESC LIMIT 1",
            (job["url"],),
        )
        if closure_signal:
            return Explained(
                check=body.check,
                status="rejected",
                reason=(gone or {}).get("reason", ""),
                refetched=True,
                closure_signal=closure_signal,
            )
        raise HTTPException(
            409,
            detail={"code": "NO_CONTENT", "message": "could not fetch this posting just now"},
        )
    content_row = {"input_content": fresh}
    cfg = ai_access.require_config(user)
    cfg = dataclasses.replace(cfg, params={**cfg.params, "reasoning_effort": "medium"})

    check = body.check
    filter_name = prompt_hash = None
    spec = POSTING_CHECKS.get(check)
    if spec:
        instructions, model_cls, verdict_of = (
            spec.instructions,
            spec.response_model,
            spec.verdict_of,
        )
    elif check.startswith("filter:"):
        flt = db.query_one(
            "SELECT name, prompt, on_ambiguous, prompt_hash FROM user_filters "
            "WHERE user_id = %s AND id = %s",
            (user.id, int(check.split(":", 1)[1])),
        )
        if not flt:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
        instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
        model_cls = FilterVerdict
        verdict_of = lambda p: (p.should_filter, p.reason)
        filter_name = f"user{user.id}:{flt['name']}"
        prompt_hash = flt["prompt_hash"]
        check = "custom"
    else:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_CHECK",
                "message": f"check must be one of {', '.join(POSTING_CHECKS)}, or filter:<id>",
            },
        )

    with budget.record_parse_failures(user.id, cfg.key_source, "explain", cfg.model):
        parsed, usage = await _verdicts.run_check(
            cfg,
            url=job["url"],
            check_type=check,
            instructions=instructions,
            input_text=content_row["input_content"][:60000],
            response_model=model_cls,
            verdict_of=verdict_of,
            company=job["company"],
            job_title=job["title"],
            filter_name=filter_name,
            prompt_hash=prompt_hash,
            context="explain",
        )
    budget.record_tokens(
        user.id,
        cfg.key_source,
        "explain",
        cfg.model,
        usage,
    )
    if parsed is None:
        # run_check records the 'failed' row and returns None when the model
        # produces no parseable output; the tokens are already spent, so this
        # must read as a real outcome rather than an unhandled AttributeError.
        raise HTTPException(
            502,
            detail={
                "code": "NO_VERDICT",
                "message": "the model returned no usable answer; try again",
            },
        )
    rejected, reason = verdict_of(parsed)
    return Explained(check=body.check, status="rejected" if rejected else "passed", reason=reason)
