"""Identity-neutral execution of one immutable custom-filter snapshot."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from api import ai, db
from api.ai import verdicts
from api.ai.batch_results import progress_counts
from core.answers import FilterDecision, FilterResult
from core.filters import build_custom_decision_instructions, build_custom_input
from core.store import get_content, get_contents, get_custom_result
from tasks.runtime import (
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
    batch_event_hook,
    collect_pending,
    consume_result,
    has_batch_work,
    submit_or_collect,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterSnapshot:
    name: str
    prompt: str
    on_ambiguous: str
    prompt_hash: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> FilterSnapshot:
        return cls(**{key: value[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ExecutionHooks:
    """Effects owned by the caller, never by custom-filter evaluation."""

    verdict_label: str
    key_source: str
    record_failure: Callable[[str | None], AbstractContextManager[None]]
    record_usage: Callable[[dict[str, int], str | None, bool], None]
    budget_exceeded: Callable[[], bool]
    cancelled: Callable[[], bool]
    progress: Callable[[int, int, str], None]
    complete: Callable[[], None]


async def check_filter(
    cfg: ai.AIConfig,
    job: dict[str, Any],
    content: str,
    snapshot: FilterSnapshot,
    verdict_label: str,
) -> dict[str, int] | None:
    """Run one check, unless this exact prompt and model already decided it."""
    # Model scope is load-bearing: changing models deliberately invalidates the
    # cache rather than treating another model's verdict as this model's work.
    if get_custom_result(job["url"], snapshot.prompt_hash, model=cfg.model):
        return None
    _, usage = await verdicts.run_check(
        cfg,
        url=job["url"],
        check_type="custom",
        instructions=build_custom_decision_instructions(snapshot.prompt, snapshot.on_ambiguous),
        input_text=build_custom_input(job["company"], job["title"], content),
        response_model=FilterDecision,
        verdict_of=lambda parsed: (parsed.should_filter, None),
        company=job["company"],
        job_title=job["title"],
        filter_name=verdict_label,
        prompt_hash=snapshot.prompt_hash,
        context="filter-run",
    )
    return usage


async def execute_live(
    task_id: int,
    cfg: ai.AIConfig,
    snapshot: FilterSnapshot,
    jobs: list[dict[str, Any]],
    hooks: ExecutionHooks,
) -> None:
    total = len(jobs)
    done = 0
    limiter = AdaptiveLimiter()
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def one(job: dict[str, Any]):
        frozen_content = "content" in job
        content = job.get("content") if frozen_content else get_content(job["url"])
        if not content and not frozen_content:
            content, _closure = await verdicts.refresh_content(
                job["url"],
                company=job.get("company") or "",
                job_title=job.get("title") or "",
                context="filter-run",
                scrape_sem=scrape_sem,
            )
        if not content:
            return None
        return await check_filter(cfg, job, content, snapshot, hooks.verdict_label)

    index = 0
    pending: dict[asyncio.Task, dict[str, Any]] = {}
    while index < total or pending:
        while index < total and len(pending) < limiter.limit:
            pending[asyncio.create_task(one(jobs[index]))] = jobs[index]
            index += 1
        finished, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for future in finished:
            job = pending.pop(future)
            done += 1
            try:
                with hooks.record_failure(cfg.model):
                    usage = future.result()
            except Exception as exc:
                text = str(exc).lower()
                limiter.record(error=True, rate_limited="429" in text or "rate limit" in text)
                logger.exception("Filter check failed for %s", job["url"])
                continue
            limiter.record()
            if usage:
                hooks.record_usage(usage, cfg.model, False)
            if done % 5 == 0:
                hooks.progress(done, total, snapshot.name)
        if hooks.cancelled():
            for future in pending:
                future.cancel()
            logger.info("Task %s cancelled mid-run", task_id)
            return
        if hooks.budget_exceeded():
            for future in pending:
                future.cancel()
            raise PermissionError(f"BUDGET_EXCEEDED after {done}/{total} checks")
    hooks.progress(total, total, snapshot.name)
    hooks.complete()


def result_label(unavailable: int, name: str) -> str:
    return f"{name}; {unavailable} without content, awaiting a later cycle" if unavailable else name


async def prepare_content(
    task_id: int,
    jobs: list[dict[str, Any]],
    *,
    attempted_urls: Callable[[list[str]], set],
    cancelled: Callable[[], bool],
    refresh_content: Callable[..., Awaitable[tuple[str | None, Any]]] = verdicts.refresh_content,
) -> tuple[dict[str, str], int]:
    contents = get_contents([job["url"] for job in jobs])
    missing = [job for job in jobs if job["url"] not in contents]
    attempted = attempted_urls([job["url"] for job in missing])
    pending = [job for job in missing if job["url"] not in attempted]
    semaphore = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def fetch(job: dict[str, Any]) -> None:
        async with semaphore:
            if cancelled():
                return
            try:
                content, _closure = await refresh_content(
                    job["url"],
                    company=job.get("company") or "",
                    job_title=job.get("title") or "",
                    context="filter-prepare",
                )
                if content:
                    contents[job["url"]] = content
            except Exception:
                logger.warning(
                    "filter content preparation failed for %s", job["url"], exc_info=True
                )

    await asyncio.gather(*(fetch(job) for job in pending))
    unavailable = sum(job["url"] not in contents for job in jobs)
    db.execute(
        "UPDATE tasks SET payload = payload || %s WHERE id = %s",
        (db.jsonb({"content_unavailable": unavailable}), task_id),
    )
    return contents, unavailable


async def execute_batch(
    task_id: int,
    cfg: ai.AIConfig | None,
    snapshot: FilterSnapshot,
    jobs: list[dict[str, Any]],
    hooks: ExecutionHooks,
    *,
    contents: dict[str, str],
    unavailable: int,
    purpose: str = "filter",
    max_output_tokens: int = 6000,
    complete_without_submission: bool = False,
    collect: Callable[..., Awaitable[list[Any]]] = collect_pending,
    submit: Callable[..., Awaitable[list[Any]]] = submit_or_collect,
) -> None:
    from core.batch import structured_response_spec

    existing = has_batch_work(task_id)
    instructions = build_custom_decision_instructions(snapshot.prompt, snapshot.on_ambiguous)
    specs, by_url = [], {}
    for job in jobs:
        if existing:
            by_url[job["url"]] = (job, None)
            continue
        content = contents.pop(job["url"], None)
        if not content:
            continue
        input_text = build_custom_input(job["company"], job["title"], content)
        specs.append(
            structured_response_spec(
                job["url"],
                instructions,
                input_text,
                FilterDecision,
                context={
                    "job": job,
                    "filter": snapshot.__dict__,
                    "reasoning_effort": cfg.params.get("reasoning_effort")
                    or cfg.params.get("effort")
                    if cfg
                    else None,
                },
            )
        )
        by_url[job["url"]] = (job, input_text)
    total = len(jobs)
    if not specs and not existing:
        hooks.progress(0, total, "no content-ready jobs; waiting for a later cycle")
        if complete_without_submission:
            hooks.complete()
        return
    hooks.progress(
        0,
        total,
        "collecting submitted batches"
        if existing
        else f"batch of {len(specs)} submitted (half price)",
    )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(60)
            db.execute("UPDATE tasks SET last_heartbeat = now() WHERE id = %s", (task_id,))
            if hooks.cancelled():
                raise asyncio.CancelledError

    hook = batch_event_hook(task_id, purpose, cfg.model if cfg else None, charged_to_user=True)
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        if existing:
            logger.info("Task %s: collecting previously submitted results", task_id)
            results = await collect(task_id, hook)
        else:
            assert cfg is not None
            results = await submit(
                task_id,
                specs,
                cfg.model,
                cfg.params.get("reasoning_effort", "medium"),
                max_output_tokens,
                hook,
            )
    finally:
        heartbeat_task.cancel()
    for done, result in enumerate(results, start=1):
        with consume_result(task_id, result) as receipt:
            if not receipt.pending:
                continue
            url = result.custom_id
            context = (result.request.context or {}) if result.request else {}
            stored = FilterSnapshot.from_mapping(context.get("filter") or snapshot.__dict__)
            job = context.get("job") or (by_url.get(url) or (None, None))[0]
            usage = ai.batch_usage(result.usage)
            if job is None:
                hooks.record_usage(usage, result.model, True)
                receipt.outcome = "unknown_request"
                continue
            parsed = None
            reason = f"batch: {result.error or 'no output'}"
            if not result.error and result.text:
                try:
                    parsed = FilterResult.model_validate_json(result.text)
                except ValueError:
                    reason = "batch: unparsable output"
            verdicts.record_ai_verdict(
                url=url,
                check_type="custom",
                rejected=parsed.should_filter if parsed else None,
                reason=parsed.reason if parsed else reason,
                parsed_json=result.text if parsed else None,
                usage=usage,
                model=result.model,
                provider="openai",
                key_source=hooks.key_source,
                company=job["company"],
                job_title=job["title"],
                instructions=result.request.instructions if result.request else None,
                input_text=result.request.input if result.request else None,
                filter_name=hooks.verdict_label,
                prompt_hash=stored.prompt_hash,
                context="filter-batch",
                batched=True,
                batch_id=result.batch_id,
                error=result.error,
                reasoning_effort=context.get("reasoning_effort"),
            )
            hooks.record_usage(usage, result.model, True)
            receipt.outcome = "written" if parsed else "failed"
        if done % 50 == 0:
            done_count, total_count = progress_counts(task_id)
            hooks.progress(done_count, total_count, result_label(unavailable, snapshot.name))
    done_count, total_count = progress_counts(task_id)
    hooks.progress(done_count, total_count, result_label(unavailable, snapshot.name))
    hooks.complete()
