"""User filter runs: sharding, per-job checks, and the batched variant."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import ai, budget, db, events, metrics, verdicts
from api.batch_results import progress_counts
from api.tasks import batch_policy
from api.tasks.board import (
    candidates_for,
    content_attempted_urls,
    decided_urls,
    in_flight_urls,
    materialize_passing,
)
from api.tasks.models import FilterVerdict
from api.tasks.runtime import (
    BATCH_CHUNK_SIZE,
    CHUNK_SIZE,
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
    batch_event_hook,
    cancelled,
    collect_pending,
    consume_result,
    enqueue,
    has_batch_work,
    load_config,
    parent_cancelled,
    set_progress,
    submit_or_collect,
    update_parent_progress,
)
from core.filters import build_custom_input, build_custom_instructions
from core.store import get_content, get_contents, get_custom_result

logger = logging.getLogger("jobtracker_worker")


async def _check_filter(
    cfg: ai.AIConfig,
    url: str,
    company: str,
    title: str,
    content: str,
    instructions: str,
    prompt_hash: str,
    filter_name: str,
) -> dict[str, int] | None:
    """Runs one custom-filter check via the shared verdict primitive; returns
    usage, or None when a cached verdict made the call unnecessary."""
    # Scoped to the model on purpose: a verdict from a different model is not
    # this model's verdict. The consequence is a cost cliff worth knowing about
    # before anyone lets the router hand this sweep more than one model - the
    # first cycle that picks differently sees NO cached verdicts and re-runs
    # the whole candidate set at full price. core/routing.py carries the
    # numbers; tests/test_routing.py pins the behaviour.
    if get_custom_result(url, prompt_hash, model=cfg.model):
        return None
    _, usage = await verdicts.run_check(
        cfg,
        url=url,
        check_type="custom",
        instructions=instructions,
        input_text=build_custom_input(company, title, content),
        response_model=FilterVerdict,
        verdict_of=lambda p: (p.should_filter, p.reason),
        company=company,
        job_title=title,
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        context="filter-run",
    )
    return usage


async def _process_jobs(
    task_id: int,
    user_id: int,
    ent,
    cfg,
    flt: dict[str, Any],
    jobs: list[dict[str, Any]],
    parent_id: int | None = None,
) -> None:
    instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
    total = len(jobs)
    done = 0
    limiter = AdaptiveLimiter()
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def one(job: dict[str, Any]):
        content = get_content(job["url"])
        if not content:
            content, _closure = await verdicts.refresh_content(
                job["url"],
                company=job.get("company") or "",
                job_title=job.get("title") or "",
                context="filter-run",
                scrape_sem=scrape_sem,
            )
        if not content:
            return None
        return await _check_filter(
            cfg,
            job["url"],
            job["company"],
            job["title"],
            content,
            instructions,
            flt["prompt_hash"],
            f"user{user_id}:{flt['name']}",
        )

    idx = 0
    pending: dict[asyncio.Task, dict[str, Any]] = {}
    while idx < total or pending:
        while idx < total and len(pending) < limiter.limit:
            pending[asyncio.create_task(one(jobs[idx]))] = jobs[idx]
            idx += 1
        finished, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for t in finished:
            job = pending.pop(t)
            done += 1
            try:
                with budget.record_parse_failures(user_id, cfg.key_source, "filter", cfg.model):
                    usage = t.result()
            except Exception as exc:
                # One bad job must not kill the run; the failed verdict is
                # recorded and retried later. Rate limits shrink concurrency.
                s = str(exc).lower()
                limiter.record(error=True, rate_limited="429" in s or "rate limit" in s)
                logger.exception(f"Filter check failed for {job['url']}")
                continue
            limiter.record()
            if usage:
                budget.record_tokens(user_id, cfg.key_source, "filter", cfg.model, usage)
            if done % 5 == 0:
                set_progress(task_id, done, total, flt["name"])
                if parent_id:
                    update_parent_progress(parent_id)
        if cancelled(task_id) or (parent_id and parent_cancelled(parent_id)):
            for t in pending:
                t.cancel()
            logger.info(f"Task {task_id} cancelled mid-run")
            return
        if (
            cfg.key_source == "owner"
            and ent.weekly_token_budget is not None
            and budget.spent_this_week(user_id) >= ent.weekly_token_budget
        ):
            for t in pending:
                t.cancel()
            raise PermissionError(f"BUDGET_EXCEEDED after {done}/{total} checks")
    set_progress(task_id, total, total, flt["name"])
    if parent_id:
        # Each chunk publishes what it decided. The parent materializes again
        # when it finalizes, but a parent waits on its slowest chunk, and a
        # chunk parked on a straggler batch holds every other chunk's passes
        # off the board for as long as the provider takes.
        materialize_passing(user_id)
        update_parent_progress(parent_id)


async def _run_filters(
    task_id: int,
    user_id: int,
    filters: list[dict[str, Any]],
    batched: bool = False,
    ignore_budget: bool = False,
) -> None:
    """Shard scheduled work for content preparation and batching; interactive work stays live."""
    ent, cfg = load_config(user_id, ignore_budget)
    if batched:
        batch_policy.require_config(task_id, cfg)
    held = in_flight_urls(user_id)
    candidates = [j for j in candidates_for(user_id) if j["url"] not in held]
    urls = [j["url"] for j in candidates]
    use_batch = batched
    units: list[tuple] = []
    for flt in filters:
        decided = decided_urls(urls, flt["prompt_hash"], cfg.model)
        todo = [j for j in candidates if j["url"] not in decided]
        metrics.CACHED_VERDICTS.inc(len(candidates) - len(todo))
        if use_batch and todo:
            for start in range(0, len(todo), BATCH_CHUNK_SIZE):
                units.append(("batch", flt, todo[start : start + BATCH_CHUNK_SIZE]))
            todo = []
        for start in range(0, len(todo), CHUNK_SIZE):
            units.append(("live", flt, todo[start : start + CHUNK_SIZE]))
    if not units:
        materialize_passing(user_id)
        set_progress(task_id, 0, 0, "everything already decided")
        return
    if len(units) == 1 and units[0][0] == "live":
        _, flt, jobs = units[0]
        await _process_jobs(task_id, user_id, ent, cfg, flt, jobs)
        materialize_passing(user_id)
        return
    total = sum(len(jobs) for _, _, jobs in units)
    for mode, flt, jobs in units:
        enqueue(
            "run_filter_batch_chunk" if mode == "batch" else "run_filter_chunk",
            {
                "parent_id": task_id,
                "user_id": user_id,
                "filter": {k: flt[k] for k in ("name", "prompt", "on_ambiguous", "prompt_hash")},
                "jobs": jobs,
                "ignore_budget": ignore_budget,
                "scheduled": batched,
            },
        )
    db.execute(
        "UPDATE tasks SET status = 'waiting', progress = %s WHERE id = %s AND status = 'running'",
        (
            db.jsonb({"done": 0, "total": total, "label": f"{len(units)} chunks across the fleet"}),
            task_id,
        ),
    )
    events.publish_task(task_id)


async def handle_run_filter_chunk(task_id: int, payload: dict[str, Any]) -> None:
    if batch_policy.scheduled(payload):
        await handle_run_filter_batch_chunk(task_id, payload)
        return
    ent, cfg = load_config(payload["user_id"], bool(payload.get("ignore_budget")))
    await _process_jobs(
        task_id,
        payload["user_id"],
        ent,
        cfg,
        payload["filter"],
        payload["jobs"],
        parent_id=payload["parent_id"],
    )


def _result_label(unavailable: int, name: str) -> str:
    return f"{name}; {unavailable} without content, awaiting a later cycle" if unavailable else name


async def _prepare_content(task_id: int, jobs: list[dict[str, Any]]) -> tuple[dict[str, str], int]:
    contents = get_contents([job["url"] for job in jobs])
    missing = [job for job in jobs if job["url"] not in contents]
    attempted = content_attempted_urls([job["url"] for job in missing])
    pending = [job for job in missing if job["url"] not in attempted]
    semaphore = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def fetch(job: dict[str, Any]) -> None:
        async with semaphore:
            if cancelled(task_id):
                return
            try:
                content, _closure = await verdicts.refresh_content(
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


async def handle_run_filter_batch_chunk(task_id: int, payload: dict[str, Any]) -> None:
    """Centralized half-price path: one worker submits the whole chunk to the
    OpenAI Batch API (core/batch.py enforces the enqueued-token budget in
    waves) and records every verdict when results land."""
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    user_id = payload["user_id"]
    flt = payload["filter"]
    jobs = payload["jobs"]
    parent_id = payload["parent_id"]
    existing = has_batch_work(task_id)
    cfg = None
    contents = {}
    unavailable = int(payload.get("content_unavailable") or 0)
    if not existing:
        ent, cfg = load_config(user_id, bool(payload.get("ignore_budget")))
        if batch_policy.scheduled(payload):
            batch_policy.require_config(task_id, cfg)
            contents, unavailable = await _prepare_content(task_id, jobs)
            if cancelled(task_id) or (parent_id and parent_cancelled(parent_id)):
                return
        elif cfg.key_source != "owner" or cfg.provider != "openai":
            await _process_jobs(task_id, user_id, ent, cfg, flt, jobs, parent_id=parent_id)
            return
        else:
            contents = get_contents([job["url"] for job in jobs])
    instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
    schema = to_strict_json_schema(FilterVerdict)
    specs, by_url = [], {}
    for job in jobs:
        if existing:
            # The original content is not snapshotted in legacy task payloads.
            by_url[job["url"]] = (job, None)
            continue
        content = contents.pop(job["url"], None)
        if not content:
            continue
        input_text = build_custom_input(job["company"], job["title"], content)
        specs.append(
            BatchSpec(
                job["url"],
                instructions,
                input_text,
                "FilterVerdict",
                schema,
                context={
                    "job": job,
                    "filter": flt,
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
        set_progress(task_id, 0, total, "no content-ready jobs; waiting for a later cycle")
        if parent_id:
            update_parent_progress(parent_id)
        return
    label = (
        "collecting submitted batches"
        if existing
        else f"batch of {len(specs)} submitted (half price)"
    )
    set_progress(task_id, 0, total, label)
    if parent_id:
        update_parent_progress(parent_id)

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(60)
            db.execute("UPDATE tasks SET last_heartbeat = now() WHERE id = %s", (task_id,))
            if cancelled(task_id) or (parent_id and parent_cancelled(parent_id)):
                raise asyncio.CancelledError

    hb = asyncio.create_task(_heartbeat())
    # charged_to_user: the loop below books every result against this
    # user with budget.record_usage, so the hook must not book the same
    # tokens again against the fleet.
    hook = batch_event_hook(task_id, "filter", cfg.model if cfg else None, charged_to_user=True)
    try:
        if existing:
            logger.info(f"Task {task_id}: collecting previously submitted results")
            results = await collect_pending(task_id, hook)
        else:
            assert cfg is not None
            results = await submit_or_collect(
                task_id,
                specs,
                cfg.model,
                cfg.params.get("reasoning_effort", "medium"),
                6000,
                hook,
            )
    finally:
        hb.cancel()
    for done, res in enumerate(results, start=1):
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            url = res.custom_id
            context = (res.request.context or {}) if res.request else {}
            stored_filter = context.get("filter") or flt
            job = context.get("job") or (by_url.get(url) or (None, None))[0]
            usage = ai.batch_usage(res.usage)
            if job is None:
                budget.record_tokens(user_id, "owner", "filter", res.model, usage, batched=True)
                receipt.outcome = "unknown_request"
                continue
            parsed = None
            reason = f"batch: {res.error or 'no output'}"
            if not res.error and res.text:
                try:
                    parsed = FilterVerdict.model_validate_json(res.text)
                except ValueError:
                    reason = "batch: unparsable output"
            verdicts.record_ai_verdict(
                url=url,
                check_type="custom",
                rejected=parsed.should_filter if parsed else None,
                reason=parsed.reason if parsed else reason,
                parsed_json=res.text if parsed else None,
                usage=usage,
                model=res.model,
                provider="openai",
                key_source="owner",
                company=job["company"],
                job_title=job["title"],
                instructions=res.request.instructions if res.request else None,
                input_text=res.request.input if res.request else None,
                filter_name=f"user{user_id}:{stored_filter['name']}",
                prompt_hash=stored_filter["prompt_hash"],
                context="filter-batch",
                batched=True,
                batch_id=res.batch_id,
                error=res.error,
                reasoning_effort=context.get("reasoning_effort"),
            )
            budget.record_tokens(user_id, "owner", "filter", res.model, usage, batched=True)
            receipt.outcome = "written" if parsed else "failed"
        if done % 50 == 0:
            set_progress(
                task_id, *progress_counts(task_id), _result_label(unavailable, flt["name"])
            )
            if parent_id:
                update_parent_progress(parent_id)
    set_progress(task_id, *progress_counts(task_id), _result_label(unavailable, flt["name"]))
    if parent_id:
        # See _process_jobs: publish this chunk's passes without waiting on
        # the siblings still parked at the provider.
        materialize_passing(user_id)
        update_parent_progress(parent_id)


async def handle_run_filter(task_id: int, payload: dict[str, Any]) -> None:
    flt = db.query_one(
        "SELECT * FROM user_filters WHERE id = %s AND user_id = %s",
        (payload["filter_id"], payload["user_id"]),
    )
    if not flt:
        raise LookupError("unknown filter")
    await _run_filters(
        task_id,
        flt["user_id"],
        [flt],
        batched=batch_policy.scheduled(payload),
        ignore_budget=bool(payload.get("ignore_budget")),
    )


async def handle_run_all_filters(task_id: int, payload: dict[str, Any]) -> None:
    filters = db.query(
        "SELECT * FROM user_filters WHERE user_id = %s AND enabled ORDER BY id",
        (payload["user_id"],),
    )
    if filters:
        await _run_filters(
            task_id,
            payload["user_id"],
            filters,
            batched=batch_policy.scheduled(payload),
            ignore_budget=bool(payload.get("ignore_budget")),
        )
