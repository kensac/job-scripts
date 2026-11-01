"""Batch request and result shapes, sizing, and provider request lines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel
lazy from openai.lib._pydantic import to_strict_json_schema

from core import providers

BatchEndpoint = Literal["/v1/responses", "/v1/embeddings"]
BATCH_ENDPOINT: BatchEndpoint = "/v1/responses"
# https://developers.openai.com/api/reference/resources/batches
BATCH_MAX_REQUESTS = 50_000
BATCH_TOKEN_BUDGET = 1_800_000
BATCH_CHARS_PER_TOKEN = 4


@dataclass
class BatchSpec:
    custom_id: str
    instructions: str = ""
    input: str = ""
    schema_name: str = ""
    schema: dict = field(default_factory=dict)
    context: dict | None = None
    endpoint: BatchEndpoint = BATCH_ENDPOINT
    inputs: list[str] | None = None


def structured_response_spec(
    custom_id: str,
    instructions: str,
    input: str,
    response_model: type[BaseModel],
    *,
    context: dict | None = None,
) -> BatchSpec:
    """A Responses spec whose schema name and schema come from one model."""
    return BatchSpec(
        custom_id,
        instructions,
        input,
        response_model.__name__,
        to_strict_json_schema(response_model),
        context=context,
    )


@dataclass
class BatchResult:
    custom_id: str
    text: str | None = None
    usage: dict | None = None
    error: str | None = None
    batch_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    request: BatchSpec | None = None
    embedding_vectors: list[list[float]] | None = None


def _estimate_tokens(spec: BatchSpec, max_output_tokens: int) -> int:
    chars = len(spec.instructions) + (
        sum(map(len, spec.inputs)) if spec.inputs is not None else len(spec.input)
    )
    return chars // BATCH_CHARS_PER_TOKEN + (
        max_output_tokens if spec.endpoint == "/v1/responses" else 0
    )


def prompt_cache_key(instructions: str) -> str:
    """One key per distinct instruction text, so every request sharing a
    prefix asks the provider for the same cache. The provider caps the key's
    length, and the hash prefix is more than enough to keep prompts apart."""
    import hashlib

    return "jt-" + hashlib.sha256(instructions.encode()).hexdigest()[:32]


def cached_prompt(
    model: str, instructions: str, input: str, cache_policy: str | None = None
) -> dict:
    """The instructions, input and cache options of one Responses request.

    On a model with explicit prompt caching the instructions go in a developer
    block that ends at a cache breakpoint, and the implicit breakpoint is off,
    so the posting text is never written at the 1.25x write rate only to go
    unread. The block tokenizes exactly as the top-level `instructions` field;
    the measurement is in docs/agents/observability.md.

    `no_cache` (managed boards, #635) keeps explicit mode with no breakpoint:
    no reads and no writes.
    """
    known = providers.model(model)
    explicit = bool(known and known.supports_explicit_prompt_cache)
    if cache_policy is not None and (cache_policy != "no_cache" or not explicit):
        raise ValueError("unsupported prompt cache policy for this model")
    if not explicit or (not instructions and cache_policy is None):
        return {"instructions": instructions, "input": input}
    if cache_policy == "no_cache":
        # The policy rides the immutable request context so legacy receipt
        # readers still accept its snapshot during a rolling deployment.
        return {
            "instructions": instructions,
            "input": input,
            "prompt_cache_options": {"mode": "explicit"},
        }
    developer = {
        "type": "input_text",
        "text": instructions,
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }
    messages = [{"role": "developer", "content": [developer]}]
    if input:
        messages.append({"role": "user", "content": [{"type": "input_text", "text": input}]})
    return {"input": messages, "prompt_cache_options": {"mode": "explicit"}}


def _build_line(spec: BatchSpec, model: str, reasoning_effort: str, max_output_tokens: int) -> dict:
    if spec.endpoint == "/v1/embeddings":
        return {
            "custom_id": spec.custom_id,
            "method": "POST",
            "url": spec.endpoint,
            "body": {
                "model": model,
                "input": spec.inputs if spec.inputs is not None else spec.input,
                "encoding_format": "float",
            },
        }
    if spec.endpoint != "/v1/responses":
        raise ValueError(f"unsupported batch endpoint: {spec.endpoint}")
    prompt = cached_prompt(
        model,
        spec.instructions,
        spec.input,
        (spec.context or {}).get("prompt_cache_policy"),
    )
    return {
        "custom_id": spec.custom_id,
        "method": "POST",
        "url": spec.endpoint,
        "body": {
            "model": model,
            **prompt,
            "reasoning": {"effort": reasoning_effort},
            "max_output_tokens": max_output_tokens,
            "store": False,
            # Routes every request carrying the same instructions to the
            # same prompt cache, so the shared prefix is served at the
            # cached rate more often. Measured over 2026-08-07 to 09-06
            # without it: a quarter of batched input tokens were cached
            # against a ceiling of about half on the filter step.
            "prompt_cache_key": prompt_cache_key(spec.instructions),
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
