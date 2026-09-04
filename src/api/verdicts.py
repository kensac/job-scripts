from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel

from api import ai, db, metrics, telemetry
from api.ai import AIConfig
from core import pricing
from core.store import add_ai_result

logger = logging.getLogger("jobtracker_api")

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
            url,
            "failed",
            f"{check_type} check failed: {str(exc)[:100]}",
            check_type,
            error=str(exc),
            **common,
        )
        metrics.CHECKS.labels(check_type, "failed").inc()
        telemetry.capture(
            "ai_call_failed",
            properties={
                "provider": cfg.provider,
                "model": cfg.model,
                "purpose": check_type,
                "context": context,
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
                "url": url,
            },
        )
        raise
    duration_ms = int((time.monotonic() - start) * 1000)
    if parsed is None:
        add_ai_result(
            url,
            "failed",
            "AI returned no parsed response",
            check_type,
            duration_ms=duration_ms,
            **common,
        )
        metrics.CHECKS.labels(check_type, "failed").inc()
        return None, usage
    rejected, reason = verdict_of(parsed)
    status = "rejected" if rejected else "passed"
    add_ai_result(
        url,
        status,
        reason,
        check_type,
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
        url,
        status,
        reason,
        check_type,
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
    cost = pricing.estimate_cost_usd(
        model,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        cached_tokens=usage.get("cached_tokens"),
        batched=batched,
    )
    if cost is not None:
        metrics.AI_COST_USD.labels(provider, model, key_source).inc(float(cost))


def host_paced(url: str) -> bool:
    """True when this url's host has used its hourly fetch allowance.

    app_config fetch_host_limits maps a host to page fetches per hour,
    fleet-wide; a host absent from it is not paced. The allowance is counted
    from the content rows the fleet wrote for that host in the last hour,
    passed or failed, so every worker reads the same ledger and no worker
    needs to know about the others. A host that blocks bursts (www.tesla.com
    served 12 pages in an hour on 2026-09-03 and then blocked 19 of the next
    32) is drip-fed at the rate it tolerates instead of being pulled off.
    """
    limits = db.get_config("fetch_host_limits") or {}
    host = urlparse(url).netloc.lower()
    per_hour = limits.get(host)
    if not per_hour:
        return False
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM ai_queries WHERE check_type = 'content' "
        "AND created_at > now() - interval '1 hour' "
        "AND (url LIKE %(https)s OR url LIKE %(http)s)",
        {"https": f"https://{host}/%", "http": f"http://{host}/%"},
    )
    used = row["n"] if row else 0
    if used >= per_hour:
        logger.info(f"{host}: {used} of {per_hour} fetches this hour used; deferring {url}")
        telemetry.capture(
            "fetch_deferred",
            properties={"fetch_host": host, "per_hour": per_hour, "used": used, "url": url},
        )
        return True
    return False


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

    Returns (content, closure_signal). closure_signal is 'ats_gone' and only
    that: it is set when the BOARD ITSELF reports the posting deleted, which is
    a fact rather than an inference. A redirect is explicitly not a closure -
    the page comes back and the closed-check judges it.

    scrape_sem, when given, is held ONLY around the browser fetch. ATS
    resolution is cheap and is the common path, so gating it on the scrape
    budget would throttle the fast case behind the slow one."""
    from api import fetching
    from core import ats
    from core.store import add_ai_result

    ats_res = await asyncio.to_thread(ats.resolve, url)
    if ats_res.status is ats.Status.GONE:
        record_manual(
            url=url,
            check_type="closed",
            rejected=True,
            reason="ATS reports posting gone",
            company=company,
            job_title=job_title,
            context=context,
        )
        return None, "ats_gone"
    if ats_res.ok and ats_res.text and not fetching.looks_blocked(ats_res.text):
        add_ai_result(
            url,
            "passed",
            "ats text",
            "content",
            input_content=ats_res.text,
            config_name="content-cache",
        )
        return ats_res.text, None

    if host_paced(url):
        # Deferred, not failed: no row is written, so the next cycle tries
        # again once the host's hour has room. Writing 'failed' here would
        # park the posting for fetch_retry_after_hours, which is a day of
        # silence for a host that only asked to be fed slowly.
        return None, None
    # The browserless tier, when the engine config asks for it: a fetch with
    # a real Chrome fingerprint, accepted only when the page plainly came
    # back whole (api.fetching.fetch_static). Anything short of that goes to
    # the browser below, so the tier can save a browser fetch but never
    # replace one with a shell. The row says 'static', so the share each
    # engine serves is readable from the content rows.
    if db.get_config("fetch_engine") == "static_first":
        static = await fetching.fetch_static(url, int(db.get_config("static_fetch_min_chars")))
        if static:
            add_ai_result(
                url,
                "passed",
                "static",
                "content",
                input_content=static,
                config_name="content-cache",
            )
            return static, None
    if scrape_sem is not None:
        async with scrape_sem:
            content, redirected = await fetching.fetch_page(url)
    else:
        content, redirected = await fetching.fetch_page(url)
    if content:
        # A redirect is no longer a verdict. It used to record a closure here
        # without the page being read, which marked 74 live jobs dead - boards
        # rewrite posting URLs constantly and no URL comparison separates a
        # canonicalisation from a bounce. The browser followed the redirect and
        # has the page, so the closed-check reads it and decides. That costs an
        # AI call on redirecting postings and is worth it: the model can tell a
        # posting from a careers index, and a URL cannot.
        if redirected:
            logger.info(f"{url} landed elsewhere; letting the check judge the page")
        add_ai_result(
            url, "passed", "scraped", "content", input_content=content, config_name="content-cache"
        )
    else:
        # A fetch that came back with nothing (blocked, timed out, empty) left
        # no record, so every hourly ingest and every backfill tried it again:
        # fulltime had 52 such postings on 2026-09-04 and spent 20 minutes an
        # hour on them, from every worker, which is also most of the fleet's
        # block rate. The row is the memory the callers key off to wait a day
        # before retrying. No input_content, so nothing downstream reads it as
        # a page; extraction_failing counts it, which it could never do before.
        add_ai_result(
            url, "failed", "fetch returned nothing", "content", config_name="content-cache"
        )
        telemetry.capture(
            "fetch_failed",
            properties={"url": url, "fetch_host": urlparse(url).netloc.lower(), "context": context},
        )
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
        url,
        status,
        reason,
        check_type,
        company=company,
        job_title=job_title,
        config_name=context,
    )
    metrics.CHECKS.labels(check_type, status).inc()
