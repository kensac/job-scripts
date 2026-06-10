from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from openai import AsyncOpenAI
from openai.types import Batch

logger = logging.getLogger("job_tracker")

_batch_client: Optional[AsyncOpenAI] = None


def _client() -> Optional[AsyncOpenAI]:
    global _batch_client
    if _batch_client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            _batch_client = AsyncOpenAI(api_key=key)
    return _batch_client


BATCH_ENDPOINT = "/v1/responses"
BATCH_COMPLETION_WINDOW = "24h"
BATCH_TOKEN_BUDGET = 1_800_000
BATCH_CHARS_PER_TOKEN = 4
BATCH_POLL_INTERVAL = 30.0

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
    text: Optional[str] = None
    usage: Optional[dict] = None
    error: Optional[str] = None


def _estimate_tokens(spec: BatchSpec, max_output_tokens: int) -> int:
    chars = len(spec.instructions) + len(spec.input)
    return chars // BATCH_CHARS_PER_TOKEN + max_output_tokens


def _chunk_specs(
    specs: List[BatchSpec], max_output_tokens: int
) -> List[List[BatchSpec]]:
    chunks: List[List[BatchSpec]] = []
    current: List[BatchSpec] = []
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


def _extract_output_text(body: dict) -> Optional[str]:
    for item in body.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text")
    return None


async def _wait_for_batch(client: AsyncOpenAI, batch_id: str) -> Batch:
    while True:
        batch = await client.batches.retrieve(batch_id)
        counts = getattr(batch, "request_counts", None)
        logger.info(
            f"Batch {batch_id}: status={batch.status} "
            f"({getattr(counts, 'completed', '?')}/{getattr(counts, 'total', '?')} done, "
            f"{getattr(counts, 'failed', '?')} failed)"
        )
        if batch.status in _TERMINAL_STATES:
            return batch
        await asyncio.sleep(BATCH_POLL_INTERVAL)


async def _run_chunk(
    client: AsyncOpenAI,
    specs: List[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
) -> Dict[str, BatchResult]:
    results: Dict[str, BatchResult] = {spec.custom_id: BatchResult(spec.custom_id) for spec in specs}

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

    batch = await _wait_for_batch(client, batch.id)

    if batch.status != "completed":
        for spec in specs:
            results[spec.custom_id].error = f"batch {batch.status}"
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
                continue
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

    if batch.error_file_id:
        try:
            err_content = await client.files.content(batch.error_file_id)
            for line in err_content.text.splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                custom_id = obj.get("custom_id")
                result = results.get(custom_id)
                if result is not None and result.text is None:
                    result.error = str(obj.get("error") or "batch error")
        except Exception as exc:
            logger.warning(f"Failed to read batch error file: {exc}")

    return results


async def run_responses_batch(
    specs: List[BatchSpec],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
) -> Dict[str, BatchResult]:
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

    results: Dict[str, BatchResult] = {}
    for index, chunk in enumerate(chunks, start=1):
        logger.info(f"Batch wave {index}/{len(chunks)}: {len(chunk)} requests")
        results.update(
            await _run_chunk(client, chunk, model, reasoning_effort, max_output_tokens)
        )
    return results
