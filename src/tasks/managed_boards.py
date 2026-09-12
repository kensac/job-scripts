"""Execute one immutable managed-board run snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api import ai, db
from api import managed_board_runs as runs
from core.filters import compute_filter_hash
from core.store import get_custom_result
from tasks.filter_execution import ExecutionHooks, FilterSnapshot, execute_batch, execute_live
from tasks.runtime import cancelled, has_batch_work, pending_batch_ids, set_progress


@dataclass(frozen=True)
class _Content:
    id: int
    input_content: str


async def handle_run_managed_board(task_id: int, payload: dict[str, Any]) -> None:
    """Receive only pre-cutover live work and projection-only reuse work."""
    if payload.get("execution_mode") == "sponsor_filter_reuse":
        set_progress(task_id, 0, len(payload["jobs"]), "projecting stored filter outcomes")
        runs.replace_projection(payload)
        set_progress(task_id, len(payload["jobs"]), len(payload["jobs"]), "projected")
        return
    if (
        payload.get("execution_version") is not None
        or payload.get("inference_transport") is not None
    ):
        raise ValueError("versioned managed board work requires the batch task kind")
    await _handle_managed_filter(task_id, payload, legacy_live=True)


async def handle_run_managed_board_batch(task_id: int, payload: dict[str, Any]) -> None:
    """Receive only versioned managed-filter work admitted after the cutover."""
    if payload.get("execution_mode") != "managed_filter":
        raise ValueError("managed board batch task must use managed_filter execution")
    await _handle_managed_filter(task_id, payload, legacy_live=False)


async def _handle_managed_filter(
    task_id: int, payload: dict[str, Any], *, legacy_live: bool
) -> None:
    execution_version = payload.get("execution_version")
    transport = payload.get("inference_transport")
    if legacy_live:
        if execution_version is not None or transport is not None:
            raise ValueError("legacy managed board task cannot carry a batch execution contract")
    elif (
        execution_version != runs.MANAGED_FILTER_EXECUTION_VERSION
        or transport != runs.MANAGED_FILTER_TRANSPORT
        or not isinstance(payload.get("reasoning_effort"), str)
    ):
        raise ValueError("managed board task snapshot has an unsupported execution contract")
    if compute_filter_hash(payload["prompt"], payload["on_ambiguous"]) != payload["prompt_hash"]:
        raise ValueError("managed board task snapshot has an invalid prompt hash")
    existing = has_batch_work(task_id) if not legacy_live else False
    provider = ai.provider_of_model(payload["requested_model"])
    if provider is None:
        raise LookupError("managed board task snapshot names an unknown model")
    if not legacy_live and provider != "openai":
        raise ValueError("managed board batch snapshot names an unsupported provider")
    key = ai.server_key(provider) if not existing else ""
    if not key and not existing:
        raise LookupError("managed board task model has no server key")
    cfg = (
        None
        if existing
        else ai.AIConfig(
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
    )
    board_id = int(payload["managed_board_id"])
    snapshot = FilterSnapshot(
        name=f"managed-board:{board_id}",
        prompt=payload["prompt"],
        on_ambiguous=payload["on_ambiguous"],
        prompt_hash=payload["prompt_hash"],
    )
    content_ids = [
        job["content_query_id"]
        for job in payload["jobs"]
        if job.get("content_query_id") is not None
    ]
    frozen_contents = {
        row.id: row.input_content
        for row in db.query_as(
            _Content,
            "SELECT id, input_content FROM ai_queries "
            "WHERE id = ANY(%s) AND check_type = 'content'",
            (content_ids,),
        )
    }
    jobs = [
        {**job, "content": frozen_contents.get(job.get("content_query_id"), "")}
        for job in payload["jobs"]
    ]

    def complete() -> None:
        if not pending_batch_ids(task_id):
            runs.replace_projection(payload)

    hooks = ExecutionHooks(
        verdict_label=f"managed-board:{board_id}",
        key_source="owner",
        record_failure=lambda model: runs.record_parse_failures(board_id, model),
        record_usage=lambda usage, model, batched: runs.record_tokens(
            board_id, usage, model, batched=batched
        ),
        budget_exceeded=lambda: False,
        cancelled=lambda: cancelled(task_id),
        progress=lambda done, total, label: set_progress(task_id, done, total, label),
        complete=complete,
    )
    if legacy_live:
        assert cfg is not None
        await execute_live(task_id, cfg, snapshot, jobs, hooks)
        return
    inference_jobs = (
        jobs
        if existing
        else [
            job
            for job in jobs
            if not get_custom_result(
                job["url"], snapshot.prompt_hash, model=payload["requested_model"]
            )
        ]
    )
    await execute_batch(
        task_id,
        cfg,
        snapshot,
        inference_jobs,
        hooks,
        contents={job["url"]: job["content"] for job in inference_jobs if job["content"]},
        unavailable=sum(not job["content"] for job in inference_jobs),
        purpose="managed_board",
        # No cap, exactly as tasks/filters.py submits: execute_batch defaults
        # to 6000, and a boolean verdict has never needed more than 1,050.
        complete_without_submission=True,
    )
