from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel

PROVIDERS = ("openai", "anthropic", "openai_compatible")

DEFAULT_MODELS = {
    "openai": "gpt-5-nano",
    "anthropic": "claude-opus-5",
    "openai_compatible": None,
}

# Shown in the UI model picker; openai_compatible accepts any model string.
MODEL_CATALOG = {
    "openai": [
        {"model": "gpt-5-nano", "note": "Cheapest, used by default; fine for most filters"},
        {"model": "gpt-5-mini", "note": "Better judgment on nuanced criteria"},
        {"model": "gpt-5", "note": "Strong general model"},
        {"model": "gpt-5.6-luna", "note": "Newest small model, fast and cheap"},
        {"model": "gpt-5.6-terra", "note": "Newest mid-tier, strong quality"},
        {"model": "gpt-5.6-sol", "note": "Newest flagship, highest cost"},
    ],
    "anthropic": [
        {"model": "claude-opus-5", "note": "Most capable, best default for Anthropic keys"},
        {"model": "claude-sonnet-5", "note": "Strong quality at lower cost"},
        {"model": "claude-haiku-4-5", "note": "Fast and cheap for simple filters"},
    ],
    "openai_compatible": [],
}

OWNER_KEY_MODELS = {
    m.strip()
    for m in os.environ.get("JOBTRACKER_OWNER_KEY_MODELS", "gpt-5-nano,gpt-5-mini").split(",")
    if m.strip()
}

_SERVER_KEY_ENVS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def server_key(provider: str) -> str:
    return os.environ.get(_SERVER_KEY_ENVS.get(provider, ""), "")


def provider_of_model(model: str) -> str | None:
    for provider, models in MODEL_CATALOG.items():
        if any(m["model"] == model for m in models):
            return provider
    return None


def owner_models(unlimited: bool) -> list:
    """Models usable on the server's keys: everything for unlimited users,
    the allowlist for budgeted ones - per provider with a key configured."""
    out = []
    for provider in ("openai", "anthropic"):
        if not server_key(provider):
            continue
        for m in MODEL_CATALOG[provider]:
            if unlimited or m["model"] in OWNER_KEY_MODELS:
                out.append(m["model"])
    return sorted(out)


# USD per 1M tokens (input, output); models absent here emit no cost metric.
PRICES_PER_MTOK = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    # Sonnet 5's launch intro price ($2/$10) became the standard price.
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

_EFFORTS_OPENAI = ("minimal", "low", "medium", "high")
_EFFORTS_ANTHROPIC = ("low", "medium", "high", "xhigh", "max")

T = TypeVar("T", bound=BaseModel)


@dataclass
class AIConfig:
    provider: str
    api_key: str
    key_source: str
    model: str
    base_url: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def validate_params(provider: str, params: dict[str, Any]) -> str | None:
    """Returns an error message, or None when valid."""
    allowed = {"reasoning_effort", "effort", "max_output_tokens", "temperature"}
    unknown = set(params) - allowed
    if unknown:
        return f"unknown params: {sorted(unknown)}"
    if "reasoning_effort" in params and params["reasoning_effort"] not in _EFFORTS_OPENAI:
        return f"reasoning_effort must be one of {_EFFORTS_OPENAI}"
    if "effort" in params and params["effort"] not in _EFFORTS_ANTHROPIC:
        return f"effort must be one of {_EFFORTS_ANTHROPIC}"
    if "max_output_tokens" in params:
        v = params["max_output_tokens"]
        if not isinstance(v, int) or not 256 <= v <= 64000:
            return "max_output_tokens must be an integer between 256 and 64000"
    if "temperature" in params:
        if provider != "openai_compatible":
            return "temperature is only supported for openai_compatible providers"
        v = params["temperature"]
        if not isinstance(v, (int, float)) or not 0 <= v <= 2:
            return "temperature must be between 0 and 2"
    return None


def _usage_tuple(prompt: int, completion: int, total: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt or 0,
        "completion_tokens": completion or 0,
        "total_tokens": total or (prompt or 0) + (completion or 0),
    }


async def parse[T: BaseModel](
    cfg: AIConfig,
    instructions: str,
    input_text: str,
    response_model: type[T],
    timeout: float = 120.0,
) -> tuple[T | None, dict[str, int]]:
    import time as _time

    from api import metrics

    start = _time.monotonic()
    try:
        result = await _parse(cfg, instructions, input_text, response_model, timeout)
    except Exception as exc:
        s = str(exc).lower()
        outcome = "rate_limited" if ("429" in s or "rate limit" in s) else "error"
        metrics.AI_CALLS.labels(cfg.provider, cfg.model, outcome).inc()
        raise
    metrics.AI_CALLS.labels(cfg.provider, cfg.model, "ok").inc()
    metrics.AI_CALL_DURATION.labels(cfg.provider).observe(_time.monotonic() - start)
    _, usage = result
    price = PRICES_PER_MTOK.get(cfg.model)
    if price and usage:
        cost = (
            usage["prompt_tokens"] * price[0] + usage["completion_tokens"] * price[1]
        ) / 1_000_000
        metrics.AI_COST_USD.labels(cfg.provider, cfg.model, cfg.key_source).inc(cost)
    return result


async def _parse[T: BaseModel](
    cfg: AIConfig,
    instructions: str,
    input_text: str,
    response_model: type[T],
    timeout: float = 120.0,
) -> tuple[T | None, dict[str, int]]:
    if cfg.provider == "anthropic":
        client = AsyncAnthropic(api_key=cfg.api_key, timeout=timeout)
        kwargs: dict[str, Any] = {}
        if cfg.params.get("effort"):
            kwargs["output_config"] = {"effort": cfg.params["effort"]}
        response = await client.messages.parse(
            model=cfg.model,
            max_tokens=cfg.params.get("max_output_tokens", 4000),
            system=instructions,
            messages=[{"role": "user", "content": input_text}],
            output_format=response_model,
            **kwargs,
        )
        usage = _usage_tuple(
            getattr(response.usage, "input_tokens", 0),
            getattr(response.usage, "output_tokens", 0),
            0,
        )
        return response.parsed_output, usage

    client_kwargs: dict[str, Any] = {"api_key": cfg.api_key}
    if cfg.provider == "openai_compatible" and cfg.base_url:
        from api.ssrf import safe_async_client

        client_kwargs["base_url"] = cfg.base_url
        client_kwargs["http_client"] = safe_async_client()
    oa = AsyncOpenAI(**client_kwargs)

    if cfg.provider == "openai":
        response = await oa.responses.parse(
            model=cfg.model,
            instructions=instructions,
            input=input_text,
            text_format=response_model,
            reasoning={"effort": cfg.params.get("reasoning_effort", "low")},
            # Covers reasoning AND output on the Responses API; too small and
            # the JSON gets truncated mid-string after a long reasoning pass.
            max_output_tokens=cfg.params.get("max_output_tokens", 6000),
            store=False,
            timeout=timeout,
        )
        u = response.usage
        usage = _usage_tuple(
            getattr(u, "input_tokens", 0) or 0,
            getattr(u, "output_tokens", 0) or 0,
            getattr(u, "total_tokens", 0) or 0,
        )
        return response.output_parsed, usage

    completion_kwargs: dict[str, Any] = {}
    if "temperature" in cfg.params:
        completion_kwargs["temperature"] = cfg.params["temperature"]
    if "max_output_tokens" in cfg.params:
        completion_kwargs["max_tokens"] = cfg.params["max_output_tokens"]
    completion = await oa.chat.completions.parse(
        model=cfg.model,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": input_text},
        ],
        response_format=response_model,
        timeout=timeout,
        **completion_kwargs,
    )
    cu = completion.usage
    usage = _usage_tuple(
        getattr(cu, "prompt_tokens", 0) or 0,
        getattr(cu, "completion_tokens", 0) or 0,
        getattr(cu, "total_tokens", 0) or 0,
    )
    parsed = completion.choices[0].message.parsed if completion.choices else None
    return parsed, usage
