"""Identity-neutral execution of one immutable custom-filter snapshot."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from api import ai, budget, db, filter_routing, review_gate, review_gate_records
from api.ai import batch_results, verdicts
from api.ai.batch_results import progress_counts
from core import providers
from core.answers import FilterDecision, FilterResult
from core.filters import build_custom_decision_instructions, build_custom_input
from core.store import decided_custom_urls, get_contents
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

# Results per collection transaction. A chunk's round trips do not grow with
# its size (tests/test_batch_collection_chunks.py), so this bounds only what
# one transaction holds: its receipt locks and the request text it sends.
# Measured on a test database, 23,396 results took 852 round trips at 500 per
# chunk (about 18 per chunk with its progress read; 88 s at the 103 ms `oci`
# is from the database) where collecting per result took 230,353 (6.6 h).
COLLECT_CHUNK = 500


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
    record_usage: Callable[[Mapping[str, int | None], str | None, bool], None]
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
    decision_id: int | None = None,
) -> dict[str, int | None]:
    """Run one check. The caller has already excluded what is decided."""
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
        on_record=lambda query_id: review_gate_records.record_outcome(decision_id, query_id),
    )
    return usage


async def execute_live(
    task_id: int,
    cfg: ai.AIConfig,
    snapshot: FilterSnapshot,
    jobs: list[dict[str, Any]],
    hooks: ExecutionHooks,
    *,
    filter_id: int | None = None,
) -> None:
    jobs, _gate_decisions = review_gate.partition(
        task_id,
        snapshot.prompt_hash,
        jobs,
        None,
        model=cfg.model,
        transport="live",
        filter_id=filter_id,
    )
    # One read of each for the whole run, before the first paid call. Per job,
    # each cost its own BEGIN, read and COMMIT for every candidate, which a
    # worker far from the database pays at full latency (observability.md).
    # Model scope is load-bearing: changing models deliberately invalidates the
    # cache rather than treating another model's verdict as this model's work.
    decided = decided_custom_urls([job["url"] for job in jobs], snapshot.prompt_hash, cfg.model)
    stored = get_contents(
        [job["url"] for job in jobs if "content" not in job and job["url"] not in decided]
    )
    parked = verdicts.fetch_parked_urls(
        [
            job["url"]
            for job in jobs
            if "content" not in job and job["url"] not in decided and job["url"] not in stored
        ]
    )
    total = len(jobs)
    done = 0
    limiter = AdaptiveLimiter()
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def one(job: dict[str, Any]):
        if job["url"] in decided:
            return None
        frozen_content = "content" in job
        content = job.get("content") if frozen_content else stored.get(job["url"])
        if not content and not frozen_content and job["url"] not in parked:
            content, _closure = await verdicts.refresh_content(
                job["url"],
                company=job.get("company") or "",
                job_title=job.get("title") or "",
                context="filter-run",
                scrape_sem=scrape_sem,
            )
        if not content:
            return None
        return await check_filter(
            cfg,
            job,
            content,
            snapshot,
            hooks.verdict_label,
            (_gate_decisions.get(job["url"]) or {}).get("decision_id"),
        )

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
            raise PermissionError(f"{budget.BUDGET_EXCEEDED} after {done}/{total} checks")
    hooks.progress(total, total, snapshot.name)
    hooks.complete()


def result_label(unavailable: int, name: str) -> str:
    return f"{name}; {unavailable} without content, awaiting a later cycle" if unavailable else name


async def prepare_content(
    task_id: int,
    jobs: list[dict[str, Any]],
    *,
    cancelled: Callable[[], bool],
    refresh_content: Callable[..., Awaitable[tuple[str | None, Any]]] = verdicts.refresh_content,
) -> tuple[dict[str, str], int]:
    contents = get_contents([job["url"] for job in jobs])
    missing = [job for job in jobs if job["url"] not in contents]
    parked = verdicts.fetch_parked_urls([job["url"] for job in missing])
    pending = [job for job in missing if job["url"] not in parked]
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
    filter_id: int | None = None,
    collect: Callable[..., Awaitable[list[Any]]] = collect_pending,
    submit: Callable[..., Awaitable[list[Any]]] = submit_or_collect,
) -> None:
    from core.batch import structured_response_spec

    existing = has_batch_work(task_id)
    cache_policy = None
    if not existing and purpose == "managed_board" and cfg:
        known = providers.model(cfg.model)
        if (
            known
            and known.supports_explicit_prompt_cache
            and not db.get_config("managed_board_cache_writes_enabled")
        ):
            cache_policy = "no_cache"
    gate_decisions = {}
    gate_skipped = 0
    if not existing:
        before_gate = len(jobs)
        jobs, gate_decisions = review_gate.partition(
            task_id,
            snapshot.prompt_hash,
            jobs,
            contents,
            model=cfg.model if cfg else None,
            transport="batch",
            filter_id=filter_id,
            observe=lambda kept: filter_routing.observations(
                filter_routing.load_policy(),
                snapshot.prompt_hash,
                kept,
                contents,
                model=cfg.model if cfg else None,
            ),
        )
        gate_skipped = before_gate - len(jobs)
    routing = {url: decision.get("routing") for url, decision in gate_decisions.items()}
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
                    **({"prompt_cache_policy": cache_policy} if cache_policy else {}),
                    "routing": routing.get(job["url"]),
                    "review_gate": gate_decisions.get(job["url"]),
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
        label = (
            f"{gate_skipped} pre-review exclusions; {total} awaiting content"
            if gate_skipped
            else "no content-ready jobs; waiting for a later cycle"
        )
        hooks.progress(0, total, label)
        if complete_without_submission or (gate_skipped and not jobs):
            hooks.complete()
        return
    hooks.progress(
        0,
        total,
        "collecting submitted batches"
        if existing
        else f"batch of {len(specs)} submitted (half price)",
    )

    hook = batch_event_hook(task_id, purpose, cfg.model if cfg else None, charged_to_user=True)
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
    for start in range(0, len(results), COLLECT_CHUNK):
        chunk = results[start : start + COLLECT_CHUNK]
        try:
            _collect_chunk(task_id, chunk, snapshot, by_url, hooks)
        except Exception:
            # The chunk rolled back whole. Per result, everything before a
            # poisoned result commits and the poison raises for the task's
            # retry, exactly as collection behaved before it was chunked.
            logger.warning(
                "Task %s: chunk failed, collecting it per result", task_id, exc_info=True
            )
            for result in chunk:
                _collect_one(task_id, result, snapshot, by_url, hooks)
        if start + COLLECT_CHUNK < len(results):
            done_count, total_count = progress_counts(task_id)
            hooks.progress(done_count, total_count, result_label(unavailable, snapshot.name))
    done_count, total_count = progress_counts(task_id)
    hooks.progress(done_count, total_count, result_label(unavailable, snapshot.name))
    hooks.complete()


@dataclass
class _Collected:
    """What one pending result writes, decided before anything is written."""

    usage: dict[str, int | None]
    model: str | None
    outcome: str
    verdict: verdicts.Verdict | None = None
    decision_id: int | None = None


def _plan(
    result: Any,
    snapshot: FilterSnapshot,
    by_url: dict[str, tuple[dict[str, Any], str | None]],
    hooks: ExecutionHooks,
) -> _Collected:
    url = result.custom_id
    context = (result.request.context or {}) if result.request else {}
    stored = FilterSnapshot.from_mapping(context.get("filter") or snapshot.__dict__)
    job = context.get("job") or (by_url.get(url) or (None, None))[0]
    usage = ai.batch_usage(result.usage)
    if job is None:
        return _Collected(usage, result.model, "unknown_request")
    parsed = None
    reason = f"batch: {result.error or 'no output'}"
    if not result.error and result.text:
        try:
            parsed = FilterResult.model_validate_json(result.text)
        except ValueError:
            reason = "batch: unparsable output"
    verdict = verdicts.Verdict(
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
    return _Collected(
        usage,
        result.model,
        "written" if parsed else "failed",
        verdict,
        (context.get("review_gate") or {}).get("decision_id"),
    )


def _collect_chunk(
    task_id: int,
    results: list[Any],
    snapshot: FilterSnapshot,
    by_url: dict[str, tuple[dict[str, Any], str | None]],
    hooks: ExecutionHooks,
) -> None:
    """Write a chunk's verdicts, usage, outcomes and receipts in one transaction.

    Ordered so that what can fail does so before any hook runs: the hooks'
    process metrics cannot be rolled back, and a failed chunk is collected
    again per result. Each table receives its rows in result order, so ids
    match the per-result form.
    """
    with batch_results.consume_results(task_id, results) as receipts:
        collected = []
        for result, receipt in zip(results, receipts, strict=True):
            if receipt.pending:
                planned = _plan(result, snapshot, by_url, hooks)
                receipt.outcome = planned.outcome
                collected.append(planned)
        written = [(p.verdict, p.decision_id) for p in collected if p.verdict is not None]
        query_ids = verdicts.record_ai_verdicts([verdict for verdict, _ in written])
        with db.pipeline():
            for (_, decision_id), query_id in zip(written, query_ids, strict=True):
                review_gate_records.record_outcome(decision_id, query_id)
        with db.pipeline():
            for planned in collected:
                hooks.record_usage(planned.usage, planned.model, True)
    for verdict, _ in written:
        verdict.count()


def _collect_one(
    task_id: int,
    result: Any,
    snapshot: FilterSnapshot,
    by_url: dict[str, tuple[dict[str, Any], str | None]],
    hooks: ExecutionHooks,
) -> None:
    with consume_result(task_id, result) as receipt:
        if not receipt.pending:
            return
        planned = _plan(result, snapshot, by_url, hooks)
        receipt.outcome = planned.outcome
        query_id = verdicts.record_ai_verdict(planned.verdict) if planned.verdict else None
        hooks.record_usage(planned.usage, planned.model, True)
        review_gate_records.record_outcome(planned.decision_id, query_id)
