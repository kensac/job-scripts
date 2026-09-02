from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel

from core import providers, routing
from core.providers.spec import Model, StructuredOutput

PROVIDERS = (*providers.PROVIDERS, "openai_compatible")

# The first model a provider declares is its default.
DEFAULT_MODELS: dict[str, str | None] = {
    name: p.models[0].name if p.models else None for name, p in providers.PROVIDERS.items()
} | {"openai_compatible": None}

# The server's own workhorse model. Separate from DEFAULT_MODELS because that
# map is legitimately nullable - openai_compatible has no default, the user
# supplies one - while the internal batch paths need a concrete model and
# should not each re-assert that this particular entry is not None.
DEFAULT_OPENAI_MODEL: str = providers.PROVIDERS["openai"].models[0].name

# Shown in the UI model picker; openai_compatible accepts any model string.
# Projected from the registry rather than maintained beside it - a second list
# of models is a second list to forget to update.
MODEL_CATALOG: dict[str, list[dict[str, str]]] = {
    name: [{"model": m.name, "note": m.note} for m in p.models if m.selectable]
    for name, p in providers.PROVIDERS.items()
} | {"openai_compatible": []}

OWNER_KEY_MODELS = {
    m.strip()
    for m in os.environ.get("JOBTRACKER_OWNER_KEY_MODELS", "gpt-5-nano,gpt-5-mini").split(",")
    if m.strip()
}


def server_key(provider: str) -> str:
    """Delegates to core.routing, which owns the one reading of a provider's
    declared key env var. Kept as a name here because most callers are in api."""
    return routing.server_key(provider)


def provider_of_model(model: str) -> str | None:
    return providers.provider_of(model)


def owner_models(unlimited: bool) -> list:
    """Models usable on the server's keys: everything for unlimited users,
    the allowlist for budgeted ones - per provider with a key configured."""
    out = []
    for provider in providers.PROVIDERS:
        if not server_key(provider):
            continue
        for m in MODEL_CATALOG[provider]:
            if unlimited or m["model"] in OWNER_KEY_MODELS:
                out.append(m["model"])
    return sorted(out)


def _declared_efforts(provider: str, model: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The (accepts, rejects) a model declares, or the provider's union.

    A model this registry does not know is NOT validated - both tuples come
    back empty and the call goes through. That is deliberate. A wrong rejection
    blocks a model that works and surfaces as a mystery outage; a wrong
    acceptance comes back as the provider's own error naming what it supports.
    Being stricter than the vendor only makes us slower than the vendor, and a
    table that blocks a model shipped yesterday is exactly that.

    When the caller does not know which model the params are for, the union
    across the provider's declared models is the permissive answer - but it is
    now DERIVED from the per-model declarations rather than hand-maintained
    beside them, which is what let the two OpenAI generations' incompatible
    sets sit in one tuple.
    """
    declared = providers.model(model)
    if declared is not None:
        return declared.reasoning.accepts, declared.reasoning.rejects
    if model:
        # A named model this registry has never heard of: validate nothing and
        # let the provider answer. This is the case the rule exists for - a
        # model that shipped after the table was last read must not be blocked
        # by the table's ignorance.
        return (), ()
    known = providers.PROVIDERS.get(provider)
    if known is None:
        return (), ()
    accepts: set[str] = set()
    for m in known.models:
        accepts.update(m.reasoning.accepts)
    return tuple(sorted(accepts)), ()


def _effort_param(provider: str) -> str | None:
    known = providers.PROVIDERS.get(provider)
    if known is None or not known.models:
        return None
    return known.models[0].reasoning.param


T = TypeVar("T", bound=BaseModel)


@dataclass
class AIConfig:
    provider: str
    api_key: str
    key_source: str
    model: str
    base_url: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def validate_params(provider: str, params: dict[str, Any], model: str | None = None) -> str | None:
    """Returns an error message, or None when valid.

    `model` is optional because the settings route does not always know it. When
    it is given and declared, the reasoning value is checked against that
    model's own accepted set; otherwise against the provider's union.
    """
    allowed = {"reasoning_effort", "effort", "max_output_tokens", "temperature"}
    unknown = set(params) - allowed
    if unknown:
        return f"unknown params: {sorted(unknown)}"
    accepts, rejects = _declared_efforts(provider, model)
    for key in ("reasoning_effort", "effort"):
        if key not in params:
            continue
        value = params[key]
        if value in rejects:
            return f"{model or provider} rejects {key}={value!r}"
        if accepts and value not in accepts:
            return f"{key} must be one of {accepts}"
    if "max_output_tokens" in params:
        v = params["max_output_tokens"]
        if not isinstance(v, int) or not 256 <= v <= 64000:
            return "max_output_tokens must be an integer between 256 and 64000"
    if "temperature" in params:
        known = providers.PROVIDERS.get(provider)
        supported = provider == "openai_compatible" or (
            known is not None and known.supports_temperature
        )
        if not supported:
            return "temperature is not supported for this provider"
        v = params["temperature"]
        if not isinstance(v, (int, float)) or not 0 <= v <= 2:
            return "temperature must be between 0 and 2"
    return None


def _usage_tuple(
    prompt: int,
    completion: int,
    total: int,
    cached: int = 0,
    reasoning: int = 0,
) -> dict[str, int]:
    """Cached and reasoning tokens are SUBSETS of prompt and completion
    respectively, not additions - they are reported so spend can be attributed,
    never summed into the total."""
    return {
        "prompt_tokens": prompt or 0,
        "completion_tokens": completion or 0,
        "total_tokens": total or (prompt or 0) + (completion or 0),
        "cached_tokens": cached or 0,
        "reasoning_tokens": reasoning or 0,
    }


def _detail(usage: Any, container: str, field: str) -> int:
    """Providers nest the cached/reasoning counts one level down and omit the
    container entirely when the count is zero."""
    return getattr(getattr(usage, container, None), field, 0) or 0


async def parse[T: BaseModel](
    cfg: AIConfig,
    instructions: str,
    input_text: str,
    response_model: type[T],
    timeout: float = 120.0,
) -> tuple[T | None, dict[str, int]]:
    import time as _time

    from api import metrics
    from core import pricing

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
    if usage:
        cost = pricing.estimate_cost_usd(
            cfg.model,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            cached_tokens=usage.get("cached_tokens"),
        )
        if cost is not None:
            metrics.AI_COST_USD.labels(cfg.provider, cfg.model, cfg.key_source).inc(float(cost))
    return result


async def _parse_json_object[T: BaseModel](
    oa: AsyncOpenAI,
    cfg: AIConfig,
    instructions: str,
    input_text: str,
    response_model: type[T],
    declared: Model,
    timeout: float,
) -> tuple[T | None, dict[str, int]]:
    """The path for a provider that returns JSON but will not enforce a schema.

    Reached by DECLARED MODE, never by provider name. A provider is on this
    path because its datasheet says json_object, so the next one that says so
    needs no branch here - which is the difference between a router and a pile
    of if-statements.

    Three things this path has to do that the strict-schema path gets for free:

    The literal word. DeepSeek refuses the request outright without it -
    "Prompt must contain the word 'json' in some form to use 'response_format'
    of type 'json_object'" - so the instructions are amended rather than the
    caller being asked to remember, since forgetting is a 400 at call time.

    The reasoning default. Its declared default is applied when the caller has
    not chosen, because on DeepSeek an UNSET effort is not neutral: it costs 79
    extra input tokens per request against 'low'. Leaving it out would pay that
    scaffold by omission.

    The validation. The provider guarantees only that the bytes parse as JSON,
    not that they match the model asked for, so the shape is checked here. And
    a truncated response is refused rather than parsed: the JSON would fail to
    load with an error pointing at column 4,000 of a string instead of at the
    token budget that actually caused it.
    """
    spec = declared.structured_output
    if spec.requires_literal_json_in_prompt and "json" not in instructions.lower():
        instructions = f"{instructions}\n\nReturn a single json object."

    kwargs: dict[str, Any] = {}
    if "temperature" in cfg.params:
        kwargs["temperature"] = cfg.params["temperature"]
    kwargs["max_tokens"] = cfg.params.get(
        "max_output_tokens", declared.output.default_max_output_tokens
    )
    effort_param = declared.reasoning.param
    if effort_param:
        effort = cfg.params.get(effort_param, declared.reasoning.default)
        if effort:
            kwargs[effort_param] = effort

    completion = await oa.chat.completions.create(
        model=cfg.model,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": input_text},
        ],
        response_format={"type": "json_object"},
        timeout=timeout,
        **kwargs,
    )
    cu = completion.usage
    usage = _usage_tuple(
        getattr(cu, "prompt_tokens", 0) or 0,
        getattr(cu, "completion_tokens", 0) or 0,
        getattr(cu, "total_tokens", 0) or 0,
        cached=_detail(cu, "prompt_tokens_details", "cached_tokens"),
        reasoning=_detail(cu, "completion_tokens_details", "reasoning_tokens"),
    )
    choice = completion.choices[0] if completion.choices else None
    if choice is None:
        return None, usage
    truncated = declared.output.truncation_finish_reason
    if truncated is not None and choice.finish_reason == truncated:
        raise ValueError(
            f"{cfg.model} stopped at the output limit "
            f"({kwargs['max_tokens']} tokens); the response is incomplete"
        )
    content = choice.message.content
    if not content:
        return None, usage
    return response_model.model_validate_json(content), usage


async def _parse[T: BaseModel](
    cfg: AIConfig,
    instructions: str,
    input_text: str,
    response_model: type[T],
    timeout: float = 120.0,
) -> tuple[T | None, dict[str, int]]:
    if (
        providers.PROVIDERS.get(cfg.provider, None) is not None
        and providers.PROVIDERS[cfg.provider].wire is providers.Wire.ANTHROPIC_MESSAGES
    ):
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
            cached=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )
        return response.parsed_output, usage

    client_kwargs: dict[str, Any] = {"api_key": cfg.api_key}
    declared = providers.PROVIDERS.get(cfg.provider)
    if declared is not None and declared.base_url:
        # A declared provider's endpoint is our own constant, so it does not
        # need the SSRF-pinned transport below - that exists because
        # openai_compatible takes a base_url from the user.
        client_kwargs["base_url"] = declared.base_url
    elif cfg.provider == "openai_compatible" and cfg.base_url:
        from api.ssrf import safe_async_client

        client_kwargs["base_url"] = cfg.base_url
        client_kwargs["http_client"] = safe_async_client()
    oa = AsyncOpenAI(**client_kwargs)

    if declared is not None and declared.wire is providers.Wire.OPENAI_RESPONSES:
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
            cached=_detail(u, "input_tokens_details", "cached_tokens"),
            reasoning=_detail(u, "output_tokens_details", "reasoning_tokens"),
        )
        return response.output_parsed, usage

    # Among the chat-wire providers, the DECLARED MODE decides whether the
    # provider can be asked to enforce a schema. Wire chose the protocol above;
    # this chooses what to ask for within it. Dispatching on the declaration
    # rather than on the provider name is what stops the next json_object
    # provider needing a branch of its own.
    declared_model = providers.model(cfg.model)
    if (
        declared_model is not None
        and declared_model.structured_output.mode is StructuredOutput.JSON_OBJECT
    ):
        return await _parse_json_object(
            oa, cfg, instructions, input_text, response_model, declared_model, timeout
        )

    # Everything that is not OpenAI's Responses API lands here: xAI and any
    # user-configured openai_compatible endpoint. Both accept a strict
    # json_schema response_format, which is what .parse() sends, so neither
    # needs a branch of its own. DeepSeek is the provider that will: it accepts
    # only response_format json_object and rejects json_schema outright, so it
    # cannot use this path at all.
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
        cached=_detail(cu, "prompt_tokens_details", "cached_tokens"),
        reasoning=_detail(cu, "completion_tokens_details", "reasoning_tokens"),
    )
    parsed = completion.choices[0].message.parsed if completion.choices else None
    return parsed, usage
