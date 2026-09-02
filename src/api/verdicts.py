from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from api import ai, metrics
from api.ai import AIConfig
from core.store import add_ai_result

T = TypeVar("T", bound=BaseModel)

# THE single way any worker/API path runs an AI check and records its verdict.
# Every row it writes carries the full column set (company, title, model,
# duration, tokens, context) so audit surfaces never show gaps, and metrics
# are incremented in exactly one place. Do not call add_ai_result directly
# from new code paths - route them through here.


async def run_check[T: BaseModel](
    cfg: AIConfig,
    *,
    url: str,
    check_type: str,
    instructions: str,
    input_text: str,
    response_model: type[T],
    verdict_of: Callable[[T], tuple[bool, str]],
    company: str = "",
    job_title: str = "",
    filter_name: str | None = None,
    prompt_hash: str | None = None,
    context: str = "worker",
) -> tuple[T | None, dict[str, int]]:
    """Runs one structured check, records a complete verdict row + metrics.

    verdict_of maps the parsed response to (rejected, reason). Failures are
    recorded (status 'failed') and re-raised for the caller's retry policy.
    """
    common: dict[str, Any] = dict(
        model=cfg.model,
        reasoning_effort=cfg.params.get("reasoning_effort") or cfg.params.get("effort"),
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        company=company,
        job_title=job_title,
        instructions=instructions,
        input_content=input_text,
        config_name=context,
    )
    start = time.monotonic()
    try:
        parsed, usage = await ai.parse(cfg, instructions, input_text, response_model)
    except Exception as exc:
        add_ai_result(
            url, "failed", f"{check_type} check failed: {str(exc)[:100]}",
            check_type, error=str(exc), **common,
        )
        metrics.CHECKS.labels(check_type, "failed").inc()
        raise
    duration_ms = int((time.monotonic() - start) * 1000)
    if parsed is None:
        add_ai_result(
            url, "failed", "AI returned no parsed response", check_type,
            duration_ms=duration_ms, **common,
        )
        metrics.CHECKS.labels(check_type, "failed").inc()
        return None, usage
    rejected, reason = verdict_of(parsed)
    status = "rejected" if rejected else "passed"
    add_ai_result(
        url, status, reason, check_type,
        parsed_json=json.dumps(parsed.model_dump()),
        duration_ms=duration_ms,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        **common,
    )
    metrics.CHECKS.labels(check_type, status).inc()
    return parsed, usage


def record_ai_verdict(
    *,
    url: str,
    check_type: str,
    rejected: bool,
    reason: str,
    parsed_json: str,
    usage: dict[str, int],
    model: str,
    provider: str = "openai",
    key_source: str = "owner",
    company: str = "",
    job_title: str = "",
    instructions: str = "",
    input_text: str = "",
    filter_name: str | None = None,
    prompt_hash: str | None = None,
    context: str = "worker",
    batched: bool = False,
    batch_id: str | None = None,
) -> None:
    """Records a verdict whose AI response was obtained outside ai.parse
    (e.g. the Batch API) - same complete row, metrics, and cost accounting
    (batch pricing is half price)."""
    status = "rejected" if rejected else "passed"
    add_ai_result(
        url, status, reason, check_type,
        model=model,
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        company=company,
        job_title=job_title,
        instructions=instructions,
        input_content=input_text,
        parsed_json=parsed_json,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        config_name=context,
        batch_id=batch_id,
    )
    metrics.CHECKS.labels(check_type, status).inc()
    metrics.AI_CALLS.labels(provider, model, "ok").inc()
    price = ai.PRICES_PER_MTOK.get(model)
    if price:
        mult = 0.5 if batched else 1.0
        cost = (
            usage.get("prompt_tokens", 0) * price[0]
            + usage.get("completion_tokens", 0) * price[1]
        ) * mult / 1_000_000
        metrics.AI_COST_USD.labels(provider, model, key_source).inc(cost)


async def refresh_content(
    url: str,
    company: str = "",
    job_title: str = "",
    context: str = "manual",
    scrape_sem: asyncio.Semaphore | None = None,
) -> tuple[str | None, str | None]:
    """Re-fetches a posting and returns fresh text, or None when the posting is
    gone. A 'recheck' that reuses cached text can only ever re-run the model
    over the page as it looked before it closed — it cannot discover a closure,
    which is the one thing a recheck is usually asked to do.

    Returns (content, closure_signal). closure_signal names WHY the posting is
    gone ('ats_gone' | 'redirected_away') and is set only when a closed verdict
    was recorded; callers should report it explicitly rather than inferring
    closure from incidental values like a zero token count.

    scrape_sem, when given, is held ONLY around the browser fetch. ATS
    resolution is cheap and is the common path, so gating it on the scrape
    budget would throttle the fast case behind the slow one."""
    from api import fetching
    from core import ats
    from core.store import add_ai_result

    ats_res = await asyncio.to_thread(ats.resolve, url)
    if ats_res.status is ats.Status.GONE:
        record_manual(
            url=url, check_type="closed", rejected=True,
            reason="ATS reports posting gone", company=company,
            job_title=job_title, context=context,
        )
        return None, "ats_gone"
    if ats_res.ok and ats_res.text and not fetching.looks_blocked(ats_res.text):
        add_ai_result(url, "passed", "ats text", "content",
                      input_content=ats_res.text, config_name="content-cache")
        return ats_res.text, None

    if scrape_sem is not None:
        async with scrape_sem:
            content, redirected = await fetching.fetch_page(url)
    else:
        content, redirected = await fetching.fetch_page(url)
    if redirected:
        record_manual(
            url=url, check_type="closed", rejected=True,
            reason="posting redirects away (board index or careers page)",
            company=company, job_title=job_title, context=context,
        )
        return None, "redirected_away"
    if content:
        add_ai_result(url, "passed", "scraped", "content",
                      input_content=content, config_name="content-cache")
    return content, None


def record_manual(
    *,
    url: str,
    check_type: str,
    rejected: bool,
    reason: str,
    company: str = "",
    job_title: str = "",
    context: str = "worker",
) -> None:
    """For verdicts decided without an AI call (e.g. ATS says the posting is
    gone) - same complete row shape, same metrics."""
    status = "rejected" if rejected else "passed"
    add_ai_result(
        url, status, reason, check_type,
        company=company, job_title=job_title, config_name=context,
    )
    metrics.CHECKS.labels(check_type, status).inc()
