from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional, Tuple, Type, TypeVar

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


async def run_check(
    cfg: AIConfig,
    *,
    url: str,
    check_type: str,
    instructions: str,
    input_text: str,
    response_model: Type[T],
    verdict_of: Callable[[T], Tuple[bool, str]],
    company: str = "",
    job_title: str = "",
    filter_name: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    context: str = "worker",
) -> Tuple[Optional[T], Dict[str, int]]:
    """Runs one structured check, records a complete verdict row + metrics.

    verdict_of maps the parsed response to (rejected, reason). Failures are
    recorded (status 'failed') and re-raised for the caller's retry policy.
    """
    common: Dict[str, Any] = dict(
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
