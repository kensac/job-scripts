"""Anthropic Message Batches: submission, status and collection.

The OpenAI modules beside this one upload a file and read result files; this
vendor takes the requests inline and streams results, so it shares their
shapes (BatchSpec in, BatchResult and BatchProgress out) and none of their
transport. A batch's id says which vendor holds it, so status and collection
need no lookup.

Results are returned in the OpenAI Responses receipt shape every consumer
already reads: `usage` counts cache reads and writes inside `input_tokens`,
as OpenAI does, where this vendor reports them beside it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable
from typing import Any

lazy from anthropic import AsyncAnthropic
lazy from anthropic.lib._parse._transform import transform_schema
lazy from anthropic.types.messages import MessageBatch

from core import providers, store
from core.batch.progress import BatchProgress
from core.batch.specs import BatchResult, BatchSpec, _estimate_tokens
from core.batch.transport import BatchEventHook

logger = logging.getLogger(__name__)

BATCH_ID_PREFIX = "msgbatch_"
# The vendor's custom_id rule. A posting url never meets it, so such an id is
# sent as a digest and mapped back on collection from the task's own request
# rows (wire_ids).
_CUSTOM_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# One-hour writes, because a batch can take longer than the five-minute TTL
# and the vendor recommends the hour for batches. A write is billed once per
# shared prefix an hour, so its 2x rate is noise against the reads it buys.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}

_batch_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic | None:
    global _batch_client
    if _batch_client is None:
        key = os.environ.get(providers.PROVIDERS["anthropic"].api_key_env)
        if key:
            _batch_client = AsyncAnthropic(api_key=key)
    return _batch_client


def holds(batch_id: str) -> bool:
    return batch_id.startswith(BATCH_ID_PREFIX)


def wire_id(custom_id: str) -> str:
    if _CUSTOM_ID.match(custom_id):
        return custom_id
    return "h-" + hashlib.sha256(custom_id.encode()).hexdigest()[:40]


def wire_ids(custom_ids: Iterable[str]) -> dict[str, str]:
    """The sent id of each request, mapped to the id the caller knows it by."""
    return {wire_id(custom_id): custom_id for custom_id in custom_ids}


def request_params(spec: BatchSpec, model: str, effort: str, max_output_tokens: int) -> dict:
    if spec.endpoint != "/v1/responses":
        raise ValueError(f"{model} has no batch transport for {spec.endpoint}")
    if not spec.input:
        raise ValueError("a Messages request needs input text")
    declared = providers.model(model)
    if declared is None:
        raise ValueError(f"{model} is not declared")
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": transform_schema(spec.schema)}
    }
    if effort:
        if declared.reasoning.param != "effort" or effort not in declared.reasoning.accepts:
            raise ValueError(f"{model} does not accept effort {effort!r}")
        output_config["effort"] = effort
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": [{"role": "user", "content": spec.input}],
        "output_config": output_config,
    }
    if spec.instructions:
        block: dict[str, Any] = {"type": "text", "text": spec.instructions}
        # Managed boards ask for no cache (#635). Below the vendor's minimum
        # prefix a breakpoint is ignored without error, so it is always safe.
        if (spec.context or {}).get("prompt_cache_policy") != "no_cache":
            block["cache_control"] = CACHE_CONTROL
        params["system"] = [block]
    return params


def _status(batch: MessageBatch) -> str:
    """The vendor's state in the vocabulary ai_batches and the poller use."""
    if batch.processing_status == "ended":
        return "completed"
    if batch.processing_status == "canceling":
        return "cancelling"
    return "in_progress"


def progress(batch: MessageBatch) -> BatchProgress:
    counts = batch.request_counts
    return BatchProgress(
        status=_status(batch),
        total=counts.processing
        + counts.succeeded
        + counts.errored
        + counts.canceled
        + counts.expired,
        completed=counts.succeeded,
        failed=counts.errored + counts.canceled + counts.expired,
    )


def emit(on_event: BatchEventHook, batch: MessageBatch) -> None:
    if on_event is None:
        return
    p = progress(batch)
    try:
        on_event(
            batch.id, p.status, {"requests": p.total, "completed": p.completed, "failed": p.failed}
        )
    except Exception:
        logger.exception("batch event hook failed")


async def submit_chunk(
    anthropic: AsyncAnthropic,
    specs: list[BatchSpec],
    model: str,
    effort: str,
    max_output_tokens: int,
    on_event: BatchEventHook = None,
) -> str:
    requests = [
        {
            "custom_id": wire_id(spec.custom_id),
            "params": request_params(spec, model, effort, max_output_tokens),
        }
        for spec in specs
    ]
    if len({r["custom_id"] for r in requests}) != len(requests):
        raise ValueError("two requests in one batch share a custom_id")
    batch = await anthropic.messages.batches.create(requests=requests)  # type: ignore[arg-type]
    logger.info(f"Submitted batch {batch.id} with {len(specs)} requests")
    emit(on_event, batch)
    if on_event is not None:
        est = sum(_estimate_tokens(spec, max_output_tokens) for spec in specs)
        try:
            on_event(batch.id, _status(batch), {"est_tokens": est, "requests": len(specs)})
        except Exception:
            logger.exception("batch event hook failed")
    return batch.id


def receipt_usage(usage: Any) -> dict:
    """Vendor usage in the Responses receipt shape (core.batch.collection)."""
    read = usage.cache_read_input_tokens or 0
    written = usage.cache_creation_input_tokens or 0
    prompt = (usage.input_tokens or 0) + read + written
    return {
        "input_tokens": prompt,
        "output_tokens": usage.output_tokens or 0,
        "total_tokens": prompt + (usage.output_tokens or 0),
        "input_tokens_details": {"cached_tokens": read, "cache_write_tokens": written},
    }


async def collect(
    anthropic: AsyncAnthropic,
    batch: MessageBatch,
    results: dict[str, BatchResult],
    known: dict[str, str],
    create_missing: bool = False,
) -> dict[str, BatchResult]:
    """Read an ended batch's results into `results`, keyed by the caller's ids.

    A line whose sent id maps to no known request is kept under the sent id
    with an error rather than dropped, so the paid answer stays visible."""
    async for line in await anthropic.messages.batches.results(batch.id):
        custom_id = known.get(line.custom_id)
        result = results.get(custom_id) if custom_id else None
        if result is None:
            if not create_missing:
                continue
            result = results.setdefault(
                custom_id or line.custom_id, BatchResult(custom_id or line.custom_id)
            )
            if custom_id is None:
                result.error = "result names no submitted request"
                continue
        outcome = line.result
        if outcome.type != "succeeded":
            detail = getattr(getattr(outcome, "error", None), "error", None)
            result.error = getattr(detail, "message", None) or f"request {outcome.type}"
            continue
        message = outcome.message
        result.usage = receipt_usage(message.usage)
        result.finish_reason = message.stop_reason
        texts = [block.text for block in message.content if block.type == "text"]
        result.text = "".join(texts) if texts else None
        if result.text is None:
            result.error = "no output text"
    for result in results.values():
        if result.batch_id is None:
            result.batch_id = batch.id
    errors = {cid: r.error for cid, r in results.items() if r.error and r.batch_id == batch.id}
    if errors:
        try:
            store.record_batch_errors(batch.id, errors)
        except Exception as exc:
            logger.warning(f"Failed to record batch errors for {batch.id}: {exc}")
    return results
