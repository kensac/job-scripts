from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from openai import AsyncOpenAI
from openai.types import Batch

# on_event(batch_id, status, {"requests": n, "completed": x, "failed": y})
BatchEventHook = Callable[[str, str, dict[str, int]], None] | None

logger = logging.getLogger("job_tracker")

_batch_client: AsyncOpenAI | None = None


def _client() -> AsyncOpenAI | None:
    global _batch_client
    if _batch_client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            _batch_client = AsyncOpenAI(api_key=key)
    return _batch_client


BATCH_ENDPOINT = "/v1/responses"
BATCH_COMPLETION_WINDOW = "24h"
BATCH_TOKEN_BUDGET = 1_800_000

# How many batch waves may be in flight at once. Each wave is an independent
# OpenAI batch, and a batch spends nearly all its time queued rather than
# consuming local resources, so this bounds provider-side enqueued work rather
# than anything on our side.
BATCH_WAVE_CONCURRENCY = int(os.environ.get("JOBTRACKER_BATCH_WAVE_CONCURRENCY", "4"))
BATCH_CHARS_PER_TOKEN = 4
BATCH_POLL_INTERVAL = 30.0


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

_TERMINAL_STATES = {"completed", "failed", "expired", "cancelled"}


def batch_enabled() -> bool:
    return os.environ.get("BATCH_MODE", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class BatchSpec:
    custom_id: str
    instructions: str
    input: str
    schema_name: str
    schema: dict


@dataclass
class BatchResult:
    custom_id: str
    text: str | None = None
    usage: dict | None = None
    error: str | None = None
    batch_id: str | None = None


def _estimate_tokens(spec: BatchSpec, max_output_tokens: int) -> int:
    chars = len(spec.instructions) + len(spec.input)
    return chars // BATCH_CHARS_PER_TOKEN + max_output_tokens


def _chunk_specs(
    specs: list[BatchSpec], max_output_tokens: int
) -> list[list[BatchSpec]]:
    chunks: list[list[BatchSpec]] = []
    current: list[BatchSpec] = []
    running = 0
    for spec in specs:
        cost = _estimate_tokens(spec, max_output_tokens)
        if current and running + cost > BATCH_TOKEN_BUDGET:
            chunks.append(current)
            current = []
            running = 0
        current.append(spec)
        running += cost
    if current:
        chunks.append(current)
    return chunks


def _build_line(
    spec: BatchSpec, model: str, reasoning_effort: str, max_output_tokens: int
) -> dict:
    return {
        "custom_id": spec.custom_id,
        "method": "POST",
        "url": BATCH_ENDPOINT,
        "body": {
            "model": model,
            "instructions": spec.instructions,
            "input": spec.input,
            "reasoning": {"effort": reasoning_effort},
            "max_output_tokens": max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": spec.schema_name,
                    "strict": True,
                    "schema": spec.schema,
                }
            },
        },
    }


def _extract_output_text(body: dict) -> str | None:
    for item in body.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text")
    return None


def _emit_usage(
    on_event: BatchEventHook,
    batch_id: str,
    status: str,
    results: dict[str, BatchResult],
) -> None:
    if on_event is None:
        return
    input_tokens = output_tokens = 0
    for r in results.values():
        if r.usage:
            input_tokens += r.usage.get("input_tokens", 0) or 0
            output_tokens += r.usage.get("output_tokens", 0) or 0
    try:
        on_event(
            batch_id, status,
            {"input_tokens": input_tokens, "output_tokens": output_tokens},
        )
    except Exception:
        logger.exception("batch event hook failed")


def _emit(on_event: BatchEventHook, batch: Batch) -> None:
    if on_event is None:
        return
    counts = getattr(batch, "request_counts", None)
    try:
        on_event(
            batch.id,
            batch.status,
            {
                "requests": getattr(counts, "total", 0) or 0,
                "completed": getattr(counts, "completed", 0) or 0,
                "failed": getattr(counts, "failed", 0) or 0,
            },
        )
    except Exception:
        logger.exception("batch event hook failed")


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
        if batch.status in _TERMINAL_STATES:
            return batch
        await asyncio.sleep(BATCH_POLL_INTERVAL)


async def _collect_batch(
    client: AsyncOpenAI,
    batch: Batch,
    results: dict[str, BatchResult],
    create_missing: bool = False,
) -> dict[str, BatchResult]:
    if batch.status != "completed":
        for result in results.values():
            if result.text is None and result.error is None:
                result.error = f"batch {batch.status}"
        if not batch.output_file_id:
            return results

    if batch.output_file_id:
        content = await client.files.content(batch.output_file_id)
        for line in content.text.splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            custom_id = obj.get("custom_id")
            result = results.get(custom_id)
            if result is None:
                if not create_missing or not custom_id:
                    continue
                result = results.setdefault(custom_id, BatchResult(custom_id))
            err = obj.get("error")
            resp = obj.get("response")
            if err or not resp or resp.get("status_code") != 200:
                result.error = str(err or (resp or {}).get("status_code") or "unknown")
                continue
            body = resp.get("body", {})
            text = _extract_output_text(body)
            result.text = text
            result.usage = body.get("usage")
            if text is None:
                result.error = "no output text"

    for result in results.values():
        if result.batch_id is None:
            result.batch_id = batch.id

    if batch.error_file_id:
        try:
            err_content = await client.files.content(batch.error_file_id)
            for line in err_content.text.splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                custom_id = obj.get("custom_id")
                result = results.get(custom_id)
                if result is None and create_missing and custom_id:
                    result = results.setdefault(custom_id, BatchResult(custom_id))
                if result is not None and result.text is None:
                    result.error = str(obj.get("error") or "batch error")
        except Exception as exc:
            logger.warning(f"Failed to read batch error file: {exc}")

    return results


async def _submit_chunk(
    client: AsyncOpenAI,
    specs: list[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> str:
    """Uploads one wave and returns its provider batch id."""
    payload = "\n".join(
        json.dumps(_build_line(spec, model, reasoning_effort, max_output_tokens))
        for spec in specs
    ).encode("utf-8")

    upload = await client.files.create(
        file=("batch_input.jsonl", io.BytesIO(payload)), purpose="batch"
    )
    batch = await client.batches.create(
        input_file_id=upload.id,
        endpoint=BATCH_ENDPOINT,
        completion_window=BATCH_COMPLETION_WINDOW,
    )
    logger.info(f"Submitted batch {batch.id} with {len(specs)} requests")
    _emit(on_event, batch)
    if on_event is not None:
        est = sum(_estimate_tokens(spec, max_output_tokens) for spec in specs)
        try:
            on_event(batch.id, batch.status, {"est_tokens": est, "requests": len(specs)})
        except Exception:
            logger.exception("batch event hook failed")
    return batch.id


async def _run_chunk(
    client: AsyncOpenAI,
    specs: list[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> dict[str, BatchResult]:
    """Submit-and-wait, retained for callers that genuinely need a result in
    hand. The scheduled paths use submit_responses_batches + collect_batches
    instead so they do not hold a worker while the provider queues."""
    results: dict[str, BatchResult] = {spec.custom_id: BatchResult(spec.custom_id) for spec in specs}
    batch_id = await _submit_chunk(
        client, specs, model, reasoning_effort, max_output_tokens, on_event
    )
    batch = await _wait_for_batch(client, batch_id, on_event)
    collected = await _collect_batch(client, batch, results)
    _emit_usage(on_event, batch.id, batch.status, collected)
    return collected


async def collect_batches(
    batch_ids: list[str], on_event: BatchEventHook = None
) -> dict[str, BatchResult]:
    """Reattach to already-submitted batches (e.g. after a worker died mid-wait)
    and wait them out instead of resubmitting; results are keyed by whatever
    custom_ids the batches carry."""
    client = _client()
    if not client:
        return {}
    results: dict[str, BatchResult] = {}
    for batch_id in batch_ids:
        try:
            batch = await _wait_for_batch(client, batch_id, on_event)
        except Exception as exc:
            logger.warning(f"Batch {batch_id} reattach failed: {exc}")
            continue
        before = set(results)
        await _collect_batch(client, batch, results, create_missing=True)
        _emit_usage(
            on_event, batch.id, batch.status,
            {k: v for k, v in results.items() if k not in before},
        )
    return results


async def submit_responses_batches(
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
    client = _client()
    if not client or not specs:
        return []
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


async def batch_states(batch_ids: list[str]) -> dict[str, str]:
    """Provider status per batch id. A status call only - it never downloads
    output - so polling many batches costs about as much as polling one."""
    client = _client()
    if not client:
        return {}
    states: dict[str, str] = {}
    for batch_id in batch_ids:
        try:
            batch = await client.batches.retrieve(batch_id)
            states[batch_id] = batch.status
        except Exception as exc:
            logger.warning(f"Batch {batch_id} status check failed: {exc}")
    return states


def is_terminal(state: str) -> bool:
    return state in _TERMINAL_STATES


async def run_responses_batch(
    specs: list[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> dict[str, BatchResult]:
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
