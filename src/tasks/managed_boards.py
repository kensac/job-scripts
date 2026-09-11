"""Execute one immutable managed-board run snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api import ai, db
from api import managed_board_runs as runs
from core.filters import compute_filter_hash
from tasks.filter_execution import ExecutionHooks, FilterSnapshot, execute_live
from tasks.runtime import cancelled, set_progress


@dataclass(frozen=True)
class _Content:
    id: int
    input_content: str


async def handle_run_managed_board(task_id: int, payload: dict[str, Any]) -> None:
    if compute_filter_hash(payload["prompt"], payload["on_ambiguous"]) != payload["prompt_hash"]:
        raise ValueError("managed board task snapshot has an invalid prompt hash")
    provider = ai.provider_of_model(payload["requested_model"])
    if provider is None:
        raise LookupError("managed board task snapshot names an unknown model")
    key = ai.server_key(provider)
    if not key:
        raise LookupError("managed board task model has no server key")
    cfg = ai.AIConfig(
        provider=provider,
        api_key=key,
        key_source="owner",
        model=payload["requested_model"],
        params={"max_output_tokens": runs.FILTER_OUTPUT_RESERVATION_TOKENS},
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
        runs.replace_projection(payload)

    hooks = ExecutionHooks(
        verdict_label=f"managed-board:{board_id}",
        key_source="owner",
        record_failure=lambda model: runs.record_parse_failures(board_id, model),
        record_usage=lambda usage, model, batched: runs.record_tokens(board_id, usage, model),
        budget_exceeded=lambda: False,
        cancelled=lambda: cancelled(task_id),
        progress=lambda done, total, label: set_progress(task_id, done, total, label),
        complete=complete,
    )
    await execute_live(task_id, cfg, snapshot, jobs, hooks)
