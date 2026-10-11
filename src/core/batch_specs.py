"""Batch request and result shapes, sizing, and provider request lines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel

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
    cache_options = {}
    cache_policy = (spec.context or {}).get("prompt_cache_policy")
    if cache_policy is not None:
        known = providers.model(model)
        if cache_policy != "no_cache" or not known or not known.supports_explicit_prompt_cache:
            raise ValueError("unsupported prompt cache policy for this model")
        # No explicit breakpoints means no cache reads or writes. The policy
        # rides the immutable request context so legacy receipt readers still
        # accept its snapshot during a rolling deployment.
        cache_options = {"prompt_cache_options": {"mode": "explicit"}}
    return {
        "custom_id": spec.custom_id,
        "method": "POST",
        "url": spec.endpoint,
        "body": {
            "model": model,
            "instructions": spec.instructions,
            **cache_options,
            "input": spec.input,
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
