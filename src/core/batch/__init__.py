"""Public batch orchestration with provider calls and durable collection."""

from __future__ import annotations

import asyncio
import logging
import os

from openai import AsyncOpenAI

from core import batch_capabilities
from core.batch.collection import _collect_batch
from core.batch.progress import TERMINAL_STATES, BatchProgress, is_terminal, read_batch_progress
from core.batch.specs import (
    BATCH_CHARS_PER_TOKEN,
    BATCH_ENDPOINT,
    BATCH_MAX_REQUESTS,
    BATCH_TOKEN_BUDGET,
    BatchEndpoint,
    BatchResult,
    BatchSpec,
    _build_line,
    _estimate_tokens,
    cached_prompt,
    prompt_cache_key,
    structured_response_spec,
)
from core.batch.transport import (
    BATCH_COMPLETION_WINDOW,
    BATCH_POLL_INTERVAL,
    BatchEventCounts,
    BatchEventHook,
    _emit,
    _submit_chunk,
    _wait_for_batch,
)

logger = logging.getLogger(__name__)
_batch_client: AsyncOpenAI | None = None


def _client() -> AsyncOpenAI | None:
    global _batch_client
    if _batch_client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            _batch_client = AsyncOpenAI(api_key=key)
    return _batch_client


# How many batch waves may be in flight at once. Each wave is an independent
# OpenAI batch, and a batch spends nearly all its time queued rather than
# consuming local resources, so this bounds provider-side enqueued work rather
# than anything on our side.
BATCH_WAVE_CONCURRENCY = int(os.environ.get("JOBTRACKER_BATCH_WAVE_CONCURRENCY", "4"))


def completion_window_seconds() -> int:
    """Seconds the provider is allowed to take, read from the window we ask
    for rather than assumed. A batch that is still non-terminal past this has
    breached the provider's own guarantee and is the caller's to give up on,
    so the deadline is derived from BATCH_COMPLETION_WINDOW and moves with it.
    """
    window = BATCH_COMPLETION_WINDOW.strip().lower()
    if window.endswith("h"):
        return int(float(window[:-1]) * 3600)
    if window.endswith("d"):
        return int(float(window[:-1]) * 86400)
    if window.endswith("m"):
        return int(float(window[:-1]) * 60)
    raise ValueError(f"unrecognised completion window: {BATCH_COMPLETION_WINDOW!r}")


def _chunk_specs(specs: list[BatchSpec], max_output_tokens: int) -> list[list[BatchSpec]]:
    chunks: list[list[BatchSpec]] = []
    current: list[BatchSpec] = []
    running = inputs = 0
    for spec in specs:
        count = len(spec.inputs) if spec.inputs is not None else 1
        if count > BATCH_MAX_REQUESTS:
            raise ValueError("one request exceeds the provider embedding input limit")
        cost = _estimate_tokens(spec, max_output_tokens)
        if current and (
            running + cost > BATCH_TOKEN_BUDGET
            or len(current) >= BATCH_MAX_REQUESTS
            or inputs + count > BATCH_MAX_REQUESTS
        ):
            chunks.append(current)
            current = []
            running = inputs = 0
        current.append(spec)
        running += cost
        inputs += count
    if current:
        chunks.append(current)
    return chunks


async def _run_chunk(
    client: AsyncOpenAI,
    specs: list[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> dict[str, BatchResult]:
    """Submit-and-wait, retained for callers that genuinely need a result in
    hand. The scheduled paths use submit_batches + collect_finished_batches
    instead so they do not hold a worker while the provider queues."""
    results: dict[str, BatchResult] = {
        spec.custom_id: BatchResult(spec.custom_id) for spec in specs
    }
    batch_id = await _submit_chunk(
        client, specs, model, reasoning_effort, max_output_tokens, on_event
    )
    batch = await _wait_for_batch(client, batch_id, on_event)
    return await _collect_batch(client, batch, results)


async def submit_batches(
    specs: list[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> list[str]:
    """Creates every wave and returns their provider ids WITHOUT waiting.

    Splitting submission from collection is what lets a caller release its
    worker: a batch lives entirely provider-side while it queues, so holding a
    slot to poll it is pure waste. Waves are all created up front for the same
    reason - there is nothing local to ration, so no concurrency limit is
    needed here.
    """
    reason = batch_capabilities.unavailable_reason(model)
    if reason:
        raise ValueError(f"BATCH_UNSUPPORTED: {model}: {reason}")
    client = _client()
    if not client or not specs:
        return []
    if len({spec.endpoint for spec in specs}) != 1:
        raise ValueError("a provider submission must contain exactly one endpoint")
    chunks = _chunk_specs(specs, max_output_tokens)
    logger.info(f"Submitting {len(specs)} requests as {len(chunks)} batch(es)")
    ids: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            batch = await _submit_chunk(
                client, chunk, model, reasoning_effort, max_output_tokens, on_event
            )
        except Exception as exc:
            # A failed submission must not discard the waves already created
            # and paid for; the unsubmitted specs fall to the next sweep.
            logger.warning(f"Batch wave {index}/{len(chunks)} submission failed: {exc}")
            continue
        ids.append(batch)
    return ids


async def batch_progress(batch_ids: list[str]) -> dict[str, BatchProgress]:
    client = _client()
    if not client:
        return {}
    return await read_batch_progress(client, batch_ids)


async def run_responses_batch(
    specs: list[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> dict[str, BatchResult]:
    reason = batch_capabilities.unavailable_reason(model)
    if reason:
        raise ValueError(f"BATCH_UNSUPPORTED: {model}: {reason}")
    client = _client()
    if not client:
        return {spec.custom_id: BatchResult(spec.custom_id, error="no api key") for spec in specs}
    if not specs:
        return {}

    chunks = _chunk_specs(specs, max_output_tokens)
    logger.info(
        f"Running {len(specs)} batch requests in {len(chunks)} wave(s) "
        f"(<= {BATCH_TOKEN_BUDGET:,} tokens each)"
    )

    # Waves used to run one after another. Each is an independent OpenAI batch
    # that spends most of its life waiting, so serialising them made total time
    # the SUM of every wave's queue time: a 37-wave backfill at ~42 min a wave
    # is ~26 hours of mostly idle waiting, holding a worker slot throughout.
    # Running a bounded number concurrently collapses that to roughly the
    # slowest few waves.
    sem = asyncio.Semaphore(max(1, BATCH_WAVE_CONCURRENCY))

    async def run_wave(index: int, chunk: list[BatchSpec]) -> dict[str, BatchResult]:
        async with sem:
            logger.info(f"Batch wave {index}/{len(chunks)}: {len(chunk)} requests")
            return await _run_chunk(
                client, chunk, model, reasoning_effort, max_output_tokens, on_event
            )

    waves = await asyncio.gather(
        *(run_wave(i, c) for i, c in enumerate(chunks, start=1)),
        return_exceptions=True,
    )

    results: dict[str, BatchResult] = {}
    for index, wave in enumerate(waves, start=1):
        if isinstance(wave, BaseException):
            # One wave failing must not discard the ones that succeeded and
            # were already paid for. The unanswered specs simply get no
            # verdict, which the next sweep picks up - the same idempotency
            # the serial version relied on, just without losing siblings.
            logger.warning(f"Batch wave {index}/{len(chunks)} failed: {wave}")
            continue
        results.update(wave)
    return results


async def collect_finished_batches(
    batch_ids: list[str], on_event: BatchEventHook = None
) -> tuple[list[BatchResult], list[str]]:
    """Collect the batches that have reached a terminal state and report the
    ones that have not, without waiting on any of them.

    Waiting each id out in turn meant one straggler held a worker
    and every finished batch beside it: on 2026-09-04 fourteen filter chunks
    sat 14 hours on batches at 207 of 211 and 76 of 81 requests, with ~2,900
    already-paid verdicts uncollectable behind them. A provider batch yields
    nothing until it is terminal, so the unit of partial collection is the
    batch, not the request: take what finished, hand the rest back to be
    parked on again.
    """
    client = _client()
    if not client:
        return [], list(batch_ids)
    results: list[BatchResult] = []
    unfinished: list[str] = []
    for batch_id in batch_ids:
        try:
            batch = await client.batches.retrieve(batch_id)
        except Exception as exc:
            # Unreadable is unfinished: a transient provider error delays the
            # collection of this batch rather than dropping it.
            logger.warning(f"Batch {batch_id} status check failed: {exc}")
            unfinished.append(batch_id)
            continue
        _emit(on_event, batch)
        if batch.status not in TERMINAL_STATES:
            unfinished.append(batch_id)
            continue
        collected: dict[str, BatchResult] = {}
        await _collect_batch(client, batch, collected, create_missing=True)
        results.extend(collected.values())
    return results, unfinished


# Existing response callers and integrations retain their public entry point.
submit_responses_batches = submit_batches

__all__ = [
    "BATCH_CHARS_PER_TOKEN",
    "BATCH_COMPLETION_WINDOW",
    "BATCH_ENDPOINT",
    "BATCH_MAX_REQUESTS",
    "BATCH_POLL_INTERVAL",
    "BATCH_TOKEN_BUDGET",
    "BATCH_WAVE_CONCURRENCY",
    "BatchEndpoint",
    "BatchEventCounts",
    "BatchEventHook",
    "BatchProgress",
    "BatchResult",
    "BatchSpec",
    "_build_line",
    "_chunk_specs",
    "_client",
    "_collect_batch",
    "_emit",
    "_estimate_tokens",
    "_submit_chunk",
    "_wait_for_batch",
    "batch_progress",
    "cached_prompt",
    "collect_finished_batches",
    "completion_window_seconds",
    "is_terminal",
    "prompt_cache_key",
    "run_responses_batch",
    "structured_response_spec",
    "submit_batches",
    "submit_responses_batches",
]
