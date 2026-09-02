"""User filter runs: sharding, per-job checks, and the batched variant."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import ai, budget, db, events, metrics, verdicts
from api.tasks.board import _candidates, _content_ready_urls, _decided_urls, _materialize_passing
from api.tasks.models import FilterVerdict
from api.tasks.runtime import (
    BATCH_CHUNK_SIZE,
    CHUNK_SIZE,
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
    _batch_event_hook,
    _cancelled,
    _load_config,
    _parent_cancelled,
    _pending_batch_ids,
    _set_progress,
    _update_parent_progress,
    enqueue,
    submit_or_collect,
)
from core.filters import build_custom_instructions
from core.store import add_ai_result, get_content, get_custom_result

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
    if get_custom_result(url, prompt_hash, model=cfg.model):
        return None
    _, usage = await verdicts.run_check(
        cfg,
        url=url,
        check_type="custom",
        instructions=instructions,
        input_text=f"Company: {company}\nJob Title: {title}\n\nJob Content:\n{content}",
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
                usage = t.result()
            except Exception as exc:
                # One bad job must not kill the run; the failed verdict is
                # recorded and retried later. Rate limits shrink concurrency.
                s = str(exc).lower()
                limiter.record(error=True, rate_limited="429" in s or "rate limit" in s)
                logger.exception(f"Filter check failed for {job['url']}")
                continue
            limiter.record()
            if usage and usage["total_tokens"]:
                budget.record_usage(
                    user_id,
                    cfg.key_source,
                    "filter",
                    cfg.model,
                    usage["prompt_tokens"],
                    usage["completion_tokens"],
                    usage["total_tokens"],
                )
            if done % 5 == 0:
                _set_progress(task_id, done, total, flt["name"])
                if parent_id:
                    _update_parent_progress(parent_id)
        if _cancelled(task_id) or (parent_id and _parent_cancelled(parent_id)):
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
    _set_progress(task_id, total, total, flt["name"])
    if parent_id:
        _update_parent_progress(parent_id)


async def _run_filters(
    task_id: int, user_id: int, filters: list[dict[str, Any]], batched: bool = False
) -> None:
    """Splitter: compute the undecided work, then shard it. Scheduled (batched)
    runs send content-ready jobs through the half-price Batch API in large
    centralized chunks; jobs still needing a scrape go through live fleet
    chunks as usual (sharded parsing, centralized batching)."""
    ent, cfg = _load_config(user_id)
    candidates = _candidates(user_id)
    urls = [j["url"] for j in candidates]
    use_batch = batched and cfg.key_source == "owner" and cfg.provider == "openai"
    units: list[tuple] = []
    for flt in filters:
        decided = _decided_urls(urls, flt["prompt_hash"], cfg.model)
        todo = [j for j in candidates if j["url"] not in decided]
        metrics.CACHED_VERDICTS.inc(len(candidates) - len(todo))
        if use_batch and todo:
            ready = _content_ready_urls([j["url"] for j in todo])
            batchable = [j for j in todo if j["url"] in ready]
            todo = [j for j in todo if j["url"] not in ready]
            for start in range(0, len(batchable), BATCH_CHUNK_SIZE):
                units.append(("batch", flt, batchable[start : start + BATCH_CHUNK_SIZE]))
        for start in range(0, len(todo), CHUNK_SIZE):
            units.append(("live", flt, todo[start : start + CHUNK_SIZE]))
    if not units:
        _materialize_passing(user_id)
        _set_progress(task_id, 0, 0, "everything already decided")
        return
    if len(units) == 1 and units[0][0] == "live":
        _, flt, jobs = units[0]
        await _process_jobs(task_id, user_id, ent, cfg, flt, jobs)
        _materialize_passing(user_id)
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
    ent, cfg = _load_config(payload["user_id"])
    await _process_jobs(
        task_id,
        payload["user_id"],
        ent,
        cfg,
        payload["filter"],
        payload["jobs"],
        parent_id=payload["parent_id"],
    )


async def handle_run_filter_batch_chunk(task_id: int, payload: dict[str, Any]) -> None:
    """Centralized half-price path: one worker submits the whole chunk to the
    OpenAI Batch API (core/batch.py enforces the enqueued-token budget in
    waves) and records every verdict when results land."""
    import json as _json

    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    user_id = payload["user_id"]
    flt = payload["filter"]
    jobs = payload["jobs"]
    parent_id = payload["parent_id"]
    ent, cfg = _load_config(user_id)
    if cfg.key_source != "owner" or cfg.provider != "openai":
        # Entitlement changed since split (e.g. BYO key added): run live.
        await _process_jobs(task_id, user_id, ent, cfg, flt, jobs, parent_id=parent_id)
        return
    instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
    schema = to_strict_json_schema(FilterVerdict)
    specs, by_url = [], {}
    for job in jobs:
        content = get_content(job["url"])
        if not content:
            continue
        input_text = (
            f"Company: {job['company']}\nJob Title: {job['title']}\n\nJob Content:\n{content}"
        )
        specs.append(BatchSpec(job["url"], instructions, input_text, "FilterVerdict", schema))
        by_url[job["url"]] = (job, input_text)
    total = len(jobs)
    if not specs:
        _set_progress(task_id, total, total, "no content-ready jobs")
        if parent_id:
            _update_parent_progress(parent_id)
        return
    _set_progress(task_id, 0, total, f"batch of {len(specs)} submitted (half price)")
    if parent_id:
        _update_parent_progress(parent_id)

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(60)
            db.execute("UPDATE tasks SET last_heartbeat = now() WHERE id = %s", (task_id,))
            if _cancelled(task_id) or (parent_id and _parent_cancelled(parent_id)):
                raise asyncio.CancelledError

    hb = asyncio.create_task(_heartbeat())
    hook = _batch_event_hook(task_id, "filter", cfg.model)
    try:
        existing = _pending_batch_ids(task_id)
        if existing:
            from core.batch import collect_batches

            logger.info(f"Task {task_id}: reattaching to {len(existing)} in-flight batch(es)")
            results = await collect_batches(existing, hook)
        else:
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
    done = 0
    for url, res in results.items():
        done += 1
        job, input_text = by_url[url]
        usage = {
            "prompt_tokens": (res.usage or {}).get("input_tokens", 0),
            "completion_tokens": (res.usage or {}).get("output_tokens", 0),
            "total_tokens": (res.usage or {}).get("total_tokens", 0),
        }
        if res.error or not res.text:
            add_ai_result(
                url,
                "failed",
                f"batch: {res.error or 'no output'}",
                "custom",
                model=cfg.model,
                filter_name=f"user{user_id}:{flt['name']}",
                prompt_hash=flt["prompt_hash"],
                company=job["company"],
                job_title=job["title"],
                config_name="filter-batch",
                error=res.error,
                batch_id=res.batch_id,
            )
            metrics.CHECKS.labels("custom", "failed").inc()
            metrics.AI_CALLS.labels(cfg.provider, cfg.model, "error").inc()
            continue
        try:
            parsed = FilterVerdict(**_json.loads(res.text))
        except Exception:
            add_ai_result(
                url,
                "failed",
                "batch: unparsable output",
                "custom",
                model=cfg.model,
                prompt_hash=flt["prompt_hash"],
                company=job["company"],
                job_title=job["title"],
                config_name="filter-batch",
                batch_id=res.batch_id,
            )
            metrics.CHECKS.labels("custom", "failed").inc()
            continue
        verdicts.record_ai_verdict(
            url=url,
            check_type="custom",
            rejected=parsed.should_filter,
            reason=parsed.reason,
            parsed_json=res.text,
            usage=usage,
            model=cfg.model,
            provider=cfg.provider,
            key_source=cfg.key_source,
            company=job["company"],
            job_title=job["title"],
            instructions=instructions,
            input_text=input_text,
            filter_name=f"user{user_id}:{flt['name']}",
            prompt_hash=flt["prompt_hash"],
            context="filter-batch",
            batched=True,
            batch_id=res.batch_id,
        )
        if usage["total_tokens"]:
            budget.record_usage(
                user_id,
                cfg.key_source,
                "filter",
                cfg.model,
                usage["prompt_tokens"],
                usage["completion_tokens"],
                usage["total_tokens"],
            )
        if done % 50 == 0:
            _set_progress(task_id, done, total, flt["name"])
            if parent_id:
                _update_parent_progress(parent_id)
    _set_progress(task_id, total, total, flt["name"])
    if parent_id:
        _update_parent_progress(parent_id)


async def handle_run_filter(task_id: int, payload: dict[str, Any]) -> None:
    flt = db.query_one(
        "SELECT * FROM user_filters WHERE id = %s AND user_id = %s",
        (payload["filter_id"], payload["user_id"]),
    )
    if not flt:
        raise LookupError("unknown filter")
    await _run_filters(task_id, flt["user_id"], [flt], batched=payload.get("batched", False))


async def handle_run_all_filters(task_id: int, payload: dict[str, Any]) -> None:
    filters = db.query(
        "SELECT * FROM user_filters WHERE user_id = %s AND enabled ORDER BY id",
        (payload["user_id"],),
    )
    if filters:
        await _run_filters(
            task_id, payload["user_id"], filters, batched=payload.get("batched", False)
        )
