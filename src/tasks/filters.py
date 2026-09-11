"""User filter runs: sharding, per-job checks, and the batched variant."""

from __future__ import annotations

import logging
from typing import Any

from api import budget, db, events, metrics
from api.ai import verdicts
from api.budget import load_config
from core.store import get_contents
from tasks import batch_policy
from tasks.board import (
    candidates_for,
    content_attempted_urls,
    decided_urls,
    in_flight_urls,
    materialize_passing,
)
from tasks.filter_execution import (
    ExecutionHooks,
    FilterSnapshot,
    execute_batch,
    execute_live,
    prepare_content,
)
from tasks.runtime import (
    BATCH_CHUNK_SIZE,
    CHUNK_SIZE,
    cancelled,
    collect_pending,
    enqueue,
    has_batch_work,
    parent_cancelled,
    set_progress,
    submit_or_collect,
    update_parent_progress,
)

logger = logging.getLogger(__name__)


def _personal_hooks(
    task_id: int,
    user_id: int,
    ent,
    cfg,
    flt,
    parent_id,
    *,
    resumed_batch: bool = False,
) -> ExecutionHooks:
    def progress(done: int, total: int, label: str) -> None:
        set_progress(task_id, done, total, label)
        if parent_id:
            update_parent_progress(parent_id)

    def complete() -> None:
        if parent_id:
            materialize_passing(user_id)
            update_parent_progress(parent_id)

    key_source = "owner" if resumed_batch or cfg.key_source == "owner" else cfg.key_source
    return ExecutionHooks(
        verdict_label=f"user{user_id}:{flt['name']}",
        key_source=key_source,
        record_failure=lambda model: budget.record_parse_failures(
            user_id, key_source, "filter", model
        ),
        record_usage=lambda usage, model, batched: budget.record_tokens(
            user_id,
            "owner" if batched else key_source,
            "filter",
            model,
            usage,
            batched=batched,
        ),
        budget_exceeded=lambda: bool(
            not resumed_batch
            and key_source == "owner"
            and ent.weekly_token_budget is not None
            and budget.spent_this_week(user_id) >= ent.weekly_token_budget
        ),
        cancelled=lambda: cancelled(task_id) or bool(parent_id and parent_cancelled(parent_id)),
        progress=progress,
        complete=complete,
    )


async def _process_jobs(
    task_id: int,
    user_id: int,
    ent,
    cfg,
    flt: dict[str, Any],
    jobs: list[dict[str, Any]],
    parent_id: int | None = None,
) -> None:
    await execute_live(
        task_id,
        cfg,
        FilterSnapshot.from_mapping(flt),
        jobs,
        _personal_hooks(task_id, user_id, ent, cfg, flt, parent_id),
    )


async def _run_filters(
    task_id: int,
    user_id: int,
    filters: list[dict[str, Any]],
    batched: bool = False,
    ignore_budget: bool = False,
) -> None:
    """Shard scheduled work for content preparation and batching; interactive work stays live."""
    ent, cfg = load_config(user_id, ignore_budget)
    use_batch = batched and batch_policy.transport(task_id, cfg) == "batch"
    held = in_flight_urls(user_id)
    candidates = [j for j in candidates_for(user_id) if j["url"] not in held]
    urls = [j["url"] for j in candidates]
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


async def handle_run_filter_batch_chunk(task_id: int, payload: dict[str, Any]) -> None:
    """Centralized half-price path: one worker submits the whole chunk to the
    OpenAI Batch API (core/batch.py enforces the enqueued-token budget in
    waves) and records every verdict when results land."""
    user_id = payload["user_id"]
    flt = payload["filter"]
    jobs = payload["jobs"]
    parent_id = payload["parent_id"]
    existing = has_batch_work(task_id)
    cfg = None
    ent = None
    contents = {}
    unavailable = int(payload.get("content_unavailable") or 0)
    if not existing:
        ent, cfg = load_config(user_id, bool(payload.get("ignore_budget")))
        if batch_policy.scheduled(payload) and batch_policy.transport(task_id, cfg) == "batch":
            contents, unavailable = await prepare_content(
                task_id,
                jobs,
                attempted_urls=content_attempted_urls,
                cancelled=lambda: cancelled(task_id),
                refresh_content=verdicts.refresh_content,
            )
            if cancelled(task_id) or (parent_id and parent_cancelled(parent_id)):
                return
        elif cfg.key_source != "owner" or cfg.provider != "openai":
            await _process_jobs(task_id, user_id, ent, cfg, flt, jobs, parent_id=parent_id)
            return
        else:
            contents = get_contents([job["url"] for job in jobs])
    hooks = _personal_hooks(
        task_id,
        user_id,
        ent if not existing else None,
        cfg,
        flt,
        parent_id,
        resumed_batch=existing,
    )
    await execute_batch(
        task_id,
        cfg,
        FilterSnapshot.from_mapping(flt),
        jobs,
        hooks,
        contents=contents,
        unavailable=unavailable,
        collect=collect_pending,
        submit=submit_or_collect,
    )


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
