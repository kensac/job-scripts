"""Provider batch submission, event hooks, and terminal-state polling."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Callable
from typing import TypedDict

lazy from openai import AsyncOpenAI
lazy from openai.types import Batch

from core.batch.progress import TERMINAL_STATES, BatchProgress, progress
from core.batch.specs import BatchSpec, _build_line, _estimate_tokens

logger = logging.getLogger(__name__)
BATCH_COMPLETION_WINDOW = "24h"
BATCH_POLL_INTERVAL = 30.0


class BatchEventCounts(TypedDict, total=False):
    requests: int
    completed: int
    failed: int
    est_tokens: int


BatchEventHook = Callable[[str, str, BatchEventCounts], None] | None


def _notify(on_event: BatchEventHook, batch_id: str, status: str, counts: BatchEventCounts) -> None:
    if on_event is None:
        return
    try:
        on_event(batch_id, status, counts)
    except Exception:
        logger.exception("batch event hook failed")


def emit_progress(on_event: BatchEventHook, batch_id: str, snapshot: BatchProgress) -> None:
    counts: BatchEventCounts = {
        "requests": snapshot.total,
        "completed": snapshot.completed,
        "failed": snapshot.failed,
    }
    _notify(on_event, batch_id, snapshot.status, counts)


def emit_submitted(
    on_event: BatchEventHook,
    batch_id: str,
    status: str,
    specs: list[BatchSpec],
    max_output_tokens: int,
) -> None:
    """The estimate a new batch is recorded with, sent once on submission."""
    if on_event is None:
        return
    est = sum(_estimate_tokens(spec, max_output_tokens) for spec in specs)
    _notify(on_event, batch_id, status, {"est_tokens": est, "requests": len(specs)})


def _emit(on_event: BatchEventHook, batch: Batch) -> None:
    emit_progress(on_event, batch.id, progress(batch))


async def _wait_for_batch(
    client: AsyncOpenAI, batch_id: str, on_event: BatchEventHook = None
) -> Batch:
    while True:
        batch = await client.batches.retrieve(batch_id)
        counts = getattr(batch, "request_counts", None)
        logger.info(
            f"Batch {batch_id}: status={batch.status} "
            f"({getattr(counts, 'completed', '?')}/{getattr(counts, 'total', '?')} done, "
            f"{getattr(counts, 'failed', '?')} failed)"
        )
        _emit(on_event, batch)
        if batch.status in TERMINAL_STATES:
            return batch
        await asyncio.sleep(BATCH_POLL_INTERVAL)


async def _submit_chunk(
    client: AsyncOpenAI,
    specs: list[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> str:
    """Uploads one wave and returns its provider batch id."""
    endpoints = {spec.endpoint for spec in specs}
    if len(endpoints) != 1:
        raise ValueError("a provider batch must contain exactly one endpoint")
    endpoint = specs[0].endpoint
    payload = "\n".join(
        json.dumps(_build_line(spec, model, reasoning_effort, max_output_tokens)) for spec in specs
    ).encode("utf-8")

    upload = await client.files.create(
        file=("batch_input.jsonl", io.BytesIO(payload)), purpose="batch"
    )
    batch = await client.batches.create(
        input_file_id=upload.id,
        endpoint=endpoint,
        completion_window=BATCH_COMPLETION_WINDOW,
    )
    logger.info(f"Submitted batch {batch.id} with {len(specs)} requests")
    _emit(on_event, batch)
    emit_submitted(on_event, batch.id, batch.status, specs, max_output_tokens)
    return batch.id
