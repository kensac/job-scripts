"""Provider batch output collection and durable error receipts."""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI
from openai.types import Batch

from core import store
from core.batch_specs import BATCH_ENDPOINT, BatchResult

logger = logging.getLogger(__name__)


def _extract_output_text(body: dict) -> str | None:
    for item in body.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text")
    return None


async def _collect_batch(
    client: AsyncOpenAI,
    batch: Batch,
    results: dict[str, BatchResult],
    create_missing: bool = False,
) -> dict[str, BatchResult]:
    if batch.status != "completed":
        for result in results.values():
            if result.text is None and result.embedding_vectors is None and result.error is None:
                result.error = f"batch {batch.status}"
        if not batch.output_file_id:
            for result in results.values():
                if result.batch_id is None:
                    result.batch_id = batch.id
            _record_errors(batch, results)
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
            result.usage = body.get("usage")
            if getattr(batch, "endpoint", BATCH_ENDPOINT) == "/v1/embeddings":
                if result.usage is not None:
                    result.usage = {
                        "input_tokens": result.usage.get("prompt_tokens", 0),
                        "output_tokens": 0,
                        "total_tokens": result.usage.get("total_tokens", 0),
                    }
                data = body.get("data") or []
                if (
                    isinstance(data, list)
                    and data
                    and all(
                        isinstance(item, dict) and type(item.get("index")) is int for item in data
                    )
                    and {item["index"] for item in data} == set(range(len(data)))
                ):
                    vectors = [
                        item.get("embedding")
                        for item in sorted(data, key=lambda item: item["index"])
                    ]
                    if all(isinstance(vector, list) for vector in vectors):
                        result.embedding_vectors = vectors
                if result.embedding_vectors is None:
                    result.error = "invalid embedding vector indices"
            else:
                result.text = _extract_output_text(body)
                incomplete = body.get("incomplete_details") or {}
                result.finish_reason = incomplete.get("reason") or body.get("status")
                if result.text is None:
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
                if result is None and create_missing and custom_id:
                    result = results.setdefault(custom_id, BatchResult(custom_id))
                if result is not None and result.text is None and result.embedding_vectors is None:
                    result.error = str(obj.get("error") or "batch error")
        except Exception as exc:
            logger.warning(f"Failed to read batch error file: {exc}")

    for result in results.values():
        if result.batch_id is None:
            result.batch_id = batch.id

    _record_errors(batch, results)
    return results


def _record_errors(batch: Batch, results: dict[str, BatchResult]) -> None:
    """Every error this batch returned, stored as the provider wrote it.

    Per-request errors come from the error file. A batch-level rejection
    (validation failed before any request ran) arrives on the batch object
    with no error file at all, and is the reason for a batch that failed
    whole; it is written onto every result and stored once under an empty
    custom_id, so the whole failure keeps its reason.
    """
    batch_errors = getattr(getattr(batch, "errors", None), "data", None) or []
    messages = [getattr(e, "message", None) or str(e) for e in batch_errors]
    placeholder = f"batch {batch.status}"
    for result in results.values():
        if (
            result.text is None
            and result.embedding_vectors is None
            and messages
            and result.error in (None, "", placeholder)
        ):
            result.error = "; ".join(messages)
    errors = {cid: r.error for cid, r in results.items() if r.error and r.batch_id == batch.id}
    if messages and not errors:
        errors = {"": "; ".join(messages)}
    if not errors:
        return
    try:
        store.record_batch_errors(batch.id, errors)
    except Exception as exc:
        logger.warning(f"Failed to record batch errors for {batch.id}: {exc}")
