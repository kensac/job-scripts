"""Provider batch output collection and durable error receipts."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

lazy from openai import AsyncOpenAI
lazy from openai.types import Batch

from core import store
from core.batch.specs import BATCH_ENDPOINT, BatchResult

logger = logging.getLogger(__name__)


def _extract_output_text(body: dict) -> str | None:
    for item in body.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text")
    return None


def _line_error(obj: dict) -> str | None:
    """The provider's reason for a failed result line, wherever it sits.

    A request the provider refused over HTTP carries `error: null` and the
    reason in `response.body.error`. Reading only the top-level field stored
    all 33,365 request errors of 2026-10-31 as "batch error", during the
    hours the provider was refusing submissions for a billing hard limit.
    """
    if obj.get("error"):
        return str(obj["error"])
    resp = obj.get("response") or {}
    if resp.get("status_code") == 200:
        return None
    body = resp.get("body")
    detail = body.get("error") if isinstance(body, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else detail
    status = resp.get("status_code")
    if message:
        return f"{status}: {message}" if status else str(message)
    return str(status) if status else None


def _jsonl(text: str) -> Iterator[dict]:
    for line in text.splitlines():
        if line.strip():
            yield json.loads(line)


def _unanswered(result: BatchResult) -> bool:
    return result.text is None and result.embedding_vectors is None


def _result_for(
    results: dict[str, BatchResult], custom_id: str | None, create_missing: bool
) -> BatchResult | None:
    """The result a line answers. A line for a request the caller did not pass
    is kept only when it asked for unknown ids to be created."""
    result = results.get(custom_id) if custom_id is not None else None
    if result is None and create_missing and custom_id:
        result = results.setdefault(custom_id, BatchResult(custom_id))
    return result


def _ordered_vectors(data: object) -> list[list[float]] | None:
    """The vectors in index order, or None unless the indices are exactly
    0..n-1 and every vector is a list."""
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(item, dict) and type(item.get("index")) is int for item in data):
        return None
    if {item["index"] for item in data} != set(range(len(data))):
        return None
    vectors = [item.get("embedding") for item in sorted(data, key=lambda item: item["index"])]
    return vectors if all(isinstance(vector, list) for vector in vectors) else None


def _read_embedding(result: BatchResult, body: dict) -> None:
    result.usage = body.get("usage")
    if result.usage is not None:
        result.usage = {
            "input_tokens": result.usage.get("prompt_tokens", 0),
            "output_tokens": 0,
            "total_tokens": result.usage.get("total_tokens", 0),
        }
    vectors = _ordered_vectors(body.get("data") or [])
    if vectors is not None:
        result.embedding_vectors = vectors
    if result.embedding_vectors is None:
        result.error = "invalid embedding vector indices"


def _read_response(result: BatchResult, body: dict) -> None:
    result.usage = body.get("usage")
    result.text = _extract_output_text(body)
    incomplete = body.get("incomplete_details") or {}
    result.finish_reason = incomplete.get("reason") or body.get("status")
    if result.text is None:
        result.error = "no output text"


async def _collect_batch(
    client: AsyncOpenAI,
    batch: Batch,
    results: dict[str, BatchResult],
    create_missing: bool = False,
) -> dict[str, BatchResult]:
    if batch.output_file_id:
        embeddings = getattr(batch, "endpoint", BATCH_ENDPOINT) == "/v1/embeddings"
        content = await client.files.content(batch.output_file_id)
        for obj in _jsonl(content.text):
            result = _result_for(results, obj.get("custom_id"), create_missing)
            if result is None:
                continue
            resp = obj.get("response")
            if obj.get("error") or not resp or resp.get("status_code") != 200:
                result.error = _line_error(obj) or "unknown"
            elif embeddings:
                _read_embedding(result, resp.get("body", {}))
            else:
                _read_response(result, resp.get("body", {}))

    if batch.error_file_id:
        try:
            err_content = await client.files.content(batch.error_file_id)
            for obj in _jsonl(err_content.text):
                result = _result_for(results, obj.get("custom_id"), create_missing)
                if result is not None and _unanswered(result):
                    result.error = _line_error(obj) or "batch error"
        except Exception as exc:
            logger.warning(f"Failed to read batch error file: {exc}")

    # An expired or cancelled batch still returns the requests it finished,
    # so only a request with no answer and no error of its own is marked.
    if batch.status != "completed":
        for result in results.values():
            if _unanswered(result) and result.error is None:
                result.error = f"batch {batch.status}"

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
        if _unanswered(result) and messages and result.error in (None, "", placeholder):
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
