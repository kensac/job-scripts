"""User filter runs: sharding, per-job checks, and the batched variant."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from api import budget, db, metrics, task_jobs
from api.ai import verdicts
from api.budget import load_config
from api.model_calls import Payer
from api.task_jobs import run_jobs
from core.payload_objects import MAX_CONNECTIONS, PayloadStore
from core.screening import screen
from core.store import decided_custom_urls, get_contents
from tasks import batch_policy
from tasks.board import (
    candidates_for,
    in_flight_urls,
    materialize_passing,
    submission_exclusions,
)
from tasks.filter_execution import (
    ExecutionHooks,
    FilterSnapshot,
    execute_batch,
    execute_live,
    prepare_content,
)
from tasks.runtime import (
    cancelled,
    collect_pending,
    enqueue,
    has_batch_work,
    parent_cancelled,
    park_waiting,
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
        payer=Payer(user_id=user_id),
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
    chunk_size = int(db.get_config("filter_chunk_size"))
    batch_chunk_size = int(db.get_config("filter_batch_chunk_size"))
    recipes = db.get_config("title_screens")
    for flt in filters:
        decided = decided_custom_urls(urls, flt["prompt_hash"], cfg.model)
        todo = [j for j in candidates if j["url"] not in decided]
        metrics.CACHED_VERDICTS.inc(len(candidates) - len(todo))
        # A screened posting has no verdict, so without this every run chunked
        # it, read its page and skipped it again: 3,501 of 3,802 batch chunks
        # in the 7 days to 2026-10-10 did only that.
        if recipe := recipes.get(flt["prompt_hash"]):
            todo = [
                j for j in todo if not screen(recipe, title=j["title"], source=j["source"]).skip
            ]
        if use_batch and todo:
            for start in range(0, len(todo), batch_chunk_size):
                units.append(("batch", flt, todo[start : start + batch_chunk_size]))
            todo = []
        for start in range(0, len(todo), chunk_size):
            units.append(("live", flt, todo[start : start + chunk_size]))
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
    # A batch chunk's list is a verified object and its payload holds only the
    # reference and URLs (api.task_jobs): the chunk waits hours at the provider
    # while its payload is rewritten. Every upload finishes before the first
    # chunk exists, so a storage failure raises PayloadUnavailable with nothing
    # split or paid. A live chunk is interactive and keeps its short list inline.
    batch_lists = [jobs for mode, _, jobs in units if mode == "batch"]
    refs = []
    if batch_lists:
        store = PayloadStore.from_env()
        with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as executor:
            refs = list(executor.map(store.put_verified, batch_lists))
    batch_refs = iter(refs)
    for mode, flt, jobs in units:
        enqueue(
            "run_filter_batch_chunk" if mode == "batch" else "run_filter_chunk",
            {
                "parent_id": task_id,
                "user_id": user_id,
                "filter_id": flt["id"],
                "filter": {k: flt[k] for k in ("name", "prompt", "on_ambiguous", "prompt_hash")},
                **(
                    task_jobs.reference(task_jobs.FILTER_CHUNKS, jobs, next(batch_refs))
                    if mode == "batch"
                    else {"jobs": jobs}
                ),
                "ignore_budget": ignore_budget,
                "scheduled": batched,
            },
        )
    park_waiting(task_id, total, f"{len(units)} chunks across the fleet")


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
        run_jobs(payload),
        parent_id=payload["parent_id"],
    )


async def handle_run_filter_batch_chunk(task_id: int, payload: dict[str, Any]) -> None:
    """Centralized half-price path: one worker submits the whole chunk to the
    OpenAI Batch API (core/batch.py enforces the enqueued-token budget in
    waves) and records every verdict when results land."""
    user_id = payload["user_id"]
    flt = payload["filter"]
    jobs = run_jobs(payload)
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
    if not existing:
        assert cfg is not None
        excluded = submission_exclusions(
            task_id, user_id, [job["url"] for job in jobs], flt["prompt_hash"], cfg.model
        )
        jobs = [job for job in jobs if job["url"] not in excluded]
        if excluded:
            logger.info(
                "Filter task %s excluded %s decided or owned postings before submission",
                task_id,
                len(excluded),
            )
        if not jobs:
            hooks.progress(0, 0, "no new reviews: already decided or owned by an earlier chunk")
            hooks.complete()
            return
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
