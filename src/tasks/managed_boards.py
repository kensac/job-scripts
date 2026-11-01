"""Execute one immutable managed-board run snapshot."""

from __future__ import annotations

from typing import Any

from api import ai, db, run_configs
from api import managed_boards as runs
from api.model_calls import Payer
from api.verdict_equivalence import answering_models
from core import page_fetches, providers, routing
from core.fallback import MANAGED_BOARD_PURPOSE
from core.filters import compute_filter_hash
from core.store import decided_custom_urls
from tasks.filtering.execution import (
    ExecutionHooks,
    FilterSnapshot,
    execute_batch,
    execute_live,
    fallback_config,
    frozen_page,
)
from tasks.runtime import cancelled, has_batch_work, pending_batch_ids, set_progress


async def handle_run_managed_board(task_id: int, payload: dict[str, Any]) -> None:
    """Receive only pre-cutover live work and projection-only reuse work."""
    if payload.get("execution_mode") == "sponsor_filter_reuse":
        payload = run_configs.with_board_settings(payload)
        all_jobs = runs.run_jobs(payload)
        set_progress(task_id, 0, len(all_jobs), "projecting stored filter outcomes")
        runs.replace_projection(payload, all_jobs)
        set_progress(task_id, len(all_jobs), len(all_jobs), "projected")
        return
    if (
        payload.get("execution_version") is not None
        or payload.get("inference_transport") is not None
    ):
        raise ValueError("versioned managed board work requires the batch task kind")
    payload = _checked_snapshot(payload)
    cfg = _server_config(payload, _provider(payload))
    snapshot, jobs, hooks = _prepare_run(task_id, payload)
    await execute_live(task_id, cfg, snapshot, jobs, hooks)


async def handle_run_managed_board_batch(task_id: int, payload: dict[str, Any]) -> None:
    """Receive only versioned managed-filter work admitted after the cutover."""
    if payload.get("execution_mode") != "managed_filter":
        raise ValueError("managed board batch task must use managed_filter execution")
    if (
        payload.get("execution_version") != runs.MANAGED_FILTER_EXECUTION_VERSION
        or payload.get("inference_transport") != runs.MANAGED_FILTER_TRANSPORT
        or not isinstance(payload.get("reasoning_effort"), str)
    ):
        raise ValueError("managed board task snapshot has an unsupported execution contract")
    payload = _checked_snapshot(payload)
    # A resumed run collects what it already submitted, so it needs no key.
    resumed = has_batch_work(task_id)
    provider = _provider(payload)
    if provider != "openai":
        raise ValueError("managed board batch snapshot names an unsupported provider")
    # A board whose model's provider is out of credit runs on a model grouped
    # with it for custom verdicts, whose answers the board reads as its own.
    cfg = (
        None
        if resumed
        else fallback_config(_server_config(payload, provider), MANAGED_BOARD_PURPOSE)
    )
    snapshot, jobs, hooks = _prepare_run(task_id, payload)
    decided = (
        set()
        if resumed
        else decided_custom_urls(
            [job["url"] for job in jobs],
            snapshot.prompt_hash,
            model=answering_models("custom", payload["requested_model"]),
        )
    )
    inference_jobs = [job for job in jobs if job["url"] not in decided]
    await execute_batch(
        task_id,
        cfg,
        snapshot,
        inference_jobs,
        hooks,
        contents={
            job["url"]: page for job in inference_jobs if (page := frozen_page(job)) is not None
        },
        unavailable=sum(not job["content"] for job in inference_jobs),
        purpose="managed_board",
        # No max_output_tokens, so execute_batch's default applies, as it does
        # for tasks/filtering/personal_runs.py: a boolean verdict has never
        # needed more than 1,050.
        complete_without_submission=True,
    )


def _checked_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    payload = run_configs.with_board_settings(payload)
    if compute_filter_hash(payload["prompt"], payload["on_ambiguous"]) != payload["prompt_hash"]:
        raise ValueError("managed board task snapshot has an invalid prompt hash")
    return payload


def _provider(payload: dict[str, Any]) -> str:
    provider = providers.provider_of(payload["requested_model"])
    if provider is None:
        raise LookupError("managed board task snapshot names an unknown model")
    return provider


def _server_config(payload: dict[str, Any], provider: str) -> ai.AIConfig:
    key = routing.server_key(provider)
    if not key:
        raise LookupError("managed board task model has no server key")
    return ai.AIConfig(
        provider=provider,
        api_key=key,
        key_source="owner",
        model=payload["requested_model"],
        # No output cap, matching the user-filter path, which sets none.
        # A cap here truncated the JSON mid-string and the verdict was
        # recorded as `failed`; a board with fail_closed then drops it
        # silently. `FILTER_OUTPUT_RESERVATION_TOKENS` is the budget
        # estimate, not an instruction to the model.
        params={"reasoning_effort": payload.get("reasoning_effort") or "medium"},
    )


def _prepare_run(
    task_id: int, payload: dict[str, Any]
) -> tuple[FilterSnapshot, list[dict[str, Any]], ExecutionHooks]:
    """The run's filter, its jobs with their frozen page text, and the hooks
    that charge the board and project the run once no batch is pending."""
    board_id = int(payload["managed_board_id"])
    # Read once per run or resume: the reference costs one object GET.
    all_jobs = runs.run_jobs(payload)
    snapshot = FilterSnapshot(
        name=f"managed-board:{board_id}",
        prompt=payload["prompt"],
        on_ambiguous=payload["on_ambiguous"],
        prompt_hash=payload["prompt_hash"],
    )
    content_ids = [
        job["content_query_id"] for job in all_jobs if job.get("content_query_id") is not None
    ]
    # Keyed by the job's content_query_id, which is None for a job with no
    # content: that lookup misses and the job is checked on an empty page.
    frozen_contents: dict[int | None, str] = {
        row["id"]: row["input_content"]
        for row in page_fetches.resolve(
            db.query(
                "SELECT id, input_content, content_ref FROM page_texts WHERE id = ANY(%s)",
                (content_ids,),
            )
        )
    }
    jobs = [
        {**job, "content": frozen_contents.get(job.get("content_query_id"), "")} for job in all_jobs
    ]

    def complete() -> None:
        if not pending_batch_ids(task_id):
            runs.replace_projection(payload, all_jobs)

    hooks = ExecutionHooks(
        verdict_label=f"managed-board:{board_id}",
        key_source="owner",
        payer=Payer(managed_board_id=board_id),
        purpose="managed_board",
        record_usage=lambda usage, model, batched: runs.record_tokens(
            board_id, usage, model, batched=batched
        ),
        budget_exceeded=lambda: False,
        cancelled=lambda: cancelled(task_id),
        progress=lambda done, total, label: set_progress(task_id, done, total, label),
        complete=complete,
    )
    return snapshot, jobs, hooks
