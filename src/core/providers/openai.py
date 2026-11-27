"""OpenAI rates and limits, checked against the official model pages."""

from __future__ import annotations

import datetime
from dataclasses import replace
from decimal import Decimal

from core.providers.spec import (
    Model,
    Output,
    Provider,
    Rates,
    Reasoning,
    Source,
    StructuredOutput,
    StructuredOutputSpec,
    Tier,
    Wire,
)

_PRICES = Source(
    url="https://developers.openai.com/api/docs/models",
    read_on=datetime.date(2026, 9, 23),
    vendor=True,
    note="Reviewed the individual GPT-5, GPT-5.6 and embedding model pages",
)

_PROMPT_CACHING = Source(
    url="https://developers.openai.com/api/docs/guides/prompt-caching",
    read_on=datetime.date(2026, 9, 15),
    vendor=True,
    note="GPT-5.6 and later cache writes are 1.25x uncached input; reads are 0.1x",
)

_BATCH = Source(
    url="https://developers.openai.com/api/docs/guides/batch",
    read_on=datetime.date(2026, 9, 23),
    vendor=True,
    note="Batch processing at 50% of standard token pricing",
)

# Supported efforts probed from OpenAI 400 error responses (2026-09-02).
# Shared values across GPT-5 and GPT-5.6+ are low, medium, and high.
_EFFORTS_API = Source(
    url="https://api.openai.com/v1/responses",
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note="quoted from the provider's own 400 error text; see comment above",
)

_GPT6_PRICES = Source(
    url="https://developers.openai.com/api/docs/pricing",
    read_on=datetime.date(2026, 9, 22),
    vendor=True,
    note="short- and long-context rows for gpt-6-luna, gpt-6-sol and gpt-6-astra",
)

# Prompts above 272K input tokens rebill the full request at the long-context
# tier. Both tiers are written out explicitly per model rather than derived.
_GPT6_TIER_TOKENS = 272_000

_EARLIER_GEN = Reasoning(
    param="reasoning_effort",
    accepts=("minimal", "low", "medium", "high"),
    rejects=("none", "xhigh", "max"),
    default="low",
    source=_EFFORTS_API,
)

_5_6_GEN = Reasoning(
    param="reasoning_effort",
    accepts=("none", "low", "medium", "high", "xhigh", "max"),
    rejects=("minimal",),
    default="low",
    source=_EFFORTS_API,
)

# Probed via per-model 400s on 2026-09-22 (gpt-6-astra rejects "none" and
# "minimal"; luna/sol reject "minimal"). Note: invalid effort strings return
# the parameter enum instead of the per-model list.
_EFFORTS_API_GPT6 = Source(
    url="https://api.openai.com/v1/responses",
    read_on=datetime.date(2026, 9, 22),
    vendor=True,
    note="per-model 400 text; an invalid value returns the parameter enum instead",
)

# Default stays "low" (cheaper than OpenAI's own "medium" default).
_6_GEN = Reasoning(
    param="reasoning_effort",
    accepts=("none", "low", "medium", "high", "xhigh", "max"),
    rejects=("minimal",),
    default="low",
    source=_EFFORTS_API_GPT6,
)

_6_ASTRA = Reasoning(
    param="reasoning_effort",
    accepts=("low", "medium", "high", "xhigh", "max"),
    rejects=("none", "minimal"),
    default="low",
    source=_EFFORTS_API_GPT6,
)

# max_output_tokens covers both reasoning and output tokens on Responses API.
_OUTPUT = Output(
    max_output_tokens=128_000,
    default_max_output_tokens=6000,
    truncation_finish_reason="length",
    truncates_silently=False,
)

_OUTPUT_GPT6 = Output(
    max_output_tokens=128_000,
    default_max_output_tokens=6000,
    truncation_finish_reason="length",
    truncates_silently=False,
)

_SCHEMA = StructuredOutputSpec(
    mode=StructuredOutput.JSON_SCHEMA,
    schema_requires_all_fields_required=True,
    schema_requires_additional_properties_false=True,
)


def _rates(
    rate_in: str,
    rate_out: str,
    rate_cached_in: str,
    *,
    cache_write_multiplier: str = "1",
) -> Rates:
    return Rates(
        tiers=(
            Tier(
                None,
                Decimal(rate_in),
                Decimal(rate_out),
                Decimal(rate_cached_in),
                Decimal(rate_in) * Decimal(cache_write_multiplier),
            ),
        ),
        batch_rate=Decimal("0.5"),
        source=_PRICES,
        batch_source=_BATCH,
        cache_write_source=_PROMPT_CACHING,
    )


def _gpt6_rates(
    lo_in: str,
    lo_out: str,
    lo_cached: str,
    hi_in: str,
    hi_out: str,
    hi_cached: str,
) -> Rates:
    """Two-tier rates split at `_GPT6_TIER_TOKENS`, with 1.25x cache-write pricing."""
    return Rates(
        tiers=(
            Tier(
                _GPT6_TIER_TOKENS,
                Decimal(lo_in),
                Decimal(lo_out),
                Decimal(lo_cached),
                Decimal(lo_in) * Decimal("1.25"),
            ),
            Tier(
                None,
                Decimal(hi_in),
                Decimal(hi_out),
                Decimal(hi_cached),
                Decimal(hi_in) * Decimal("1.25"),
            ),
        ),
        batch_rate=Decimal("0.5"),
        source=_GPT6_PRICES,
        batch_source=_GPT6_PRICES,
        cache_write_source=_PROMPT_CACHING,
    )


def _long_context_rates(name: str, *values: str) -> Rates:
    return replace(
        _gpt6_rates(*values),
        source=Source(
            f"https://developers.openai.com/api/docs/models/{name}",
            datetime.date(2026, 9, 23),
            True,
        ),
        batch_source=_BATCH,
    )


PROVIDER = Provider(
    name="openai",
    wire=Wire.OPENAI_RESPONSES,
    base_url=None,
    api_key_env="OPENAI_API_KEY",
    supports_temperature=False,
    batch_endpoint="/v1/responses",
    # Match both OpenAI error codes and messages for billing exhaustion (400/429).
    exhaustion_markers=(
        "billing hard limit has been reached",
        "billing_hard_limit_reached",
        "insufficient_quota",
        "exceeded your current quota",
        "billing_not_active",
        "please check your billing details",
    ),
    source=Source(
        url="",
        read_on=None,
        vendor=False,
        note="transport facts are from this codebase's own working integration",
    ),
    models=(
        Model(
            name="gpt-5-nano",
            note="Small previous-generation model; check task quality evidence",
            context_tokens=400_000,
            structured_output=_SCHEMA,
            rates=_rates("0.05", "0.40", "0.005"),
            reasoning=_EARLIER_GEN,
            output=_OUTPUT,
        ),
        Model(
            name="gpt-5-mini",
            note="Better judgment on nuanced criteria",
            context_tokens=400_000,
            structured_output=_SCHEMA,
            rates=_rates("0.25", "2.00", "0.025"),
            reasoning=_EARLIER_GEN,
            output=_OUTPUT,
        ),
        Model(
            name="gpt-5",
            note="Strong general model",
            context_tokens=400_000,
            structured_output=_SCHEMA,
            rates=_rates("1.25", "10.00", "0.125"),
            reasoning=_EARLIER_GEN,
            output=_OUTPUT,
        ),
        Model(
            name="gpt-5.6-luna",
            note="GPT-5.6 small model, fast and cheap",
            context_tokens=1_050_000,
            structured_output=_SCHEMA,
            rates=_long_context_rates(
                "gpt-5.6-luna", "0.20", "1.20", "0.020", "0.40", "1.80", "0.040"
            ),
            reasoning=_5_6_GEN,
            output=_OUTPUT,
            # https://developers.openai.com/api/docs/guides/prompt-caching
            supports_explicit_prompt_cache=True,
        ),
        Model(
            name="gpt-5.6-terra",
            note="GPT-5.6 mid-tier, strong quality",
            context_tokens=1_050_000,
            structured_output=_SCHEMA,
            rates=_long_context_rates(
                "gpt-5.6-terra", "2.00", "12.00", "0.200", "4.00", "18.00", "0.400"
            ),
            reasoning=_5_6_GEN,
            output=_OUTPUT,
        ),
        # Embeddings only (core/embeddings.py); output rate is 0.00 because no completion tokens return.
        Model(
            name="text-embedding-3-small",
            note="Embeddings; not a chat model",
            context_tokens=None,
            structured_output=StructuredOutputSpec(mode=StructuredOutput.NONE),
            rates=_rates("0.02", "0.00", "0.02"),
            reasoning=Reasoning(param=None, accepts=(), rejects=(), default=None, source=_PRICES),
            output=Output(
                max_output_tokens=None,
                default_max_output_tokens=0,
                truncation_finish_reason=None,
                truncates_silently=False,
            ),
            selectable=False,
        ),
        Model(
            name="gpt-5.6-sol",
            note="GPT-5.6 flagship",
            context_tokens=1_050_000,
            structured_output=_SCHEMA,
            rates=_long_context_rates(
                "gpt-5.6-sol", "4.00", "20.00", "0.400", "8.00", "30.00", "0.800"
            ),
            reasoning=_5_6_GEN,
            output=_OUTPUT,
        ),
        # GPT-6 models ordered cheapest first.
        Model(
            name="gpt-6-luna",
            note="GPT-6 small: cheapest of the generation",
            context_tokens=1_050_000,
            structured_output=_SCHEMA,
            rates=_gpt6_rates("0.10", "0.50", "0.01", "0.20", "0.75", "0.02"),
            reasoning=_6_GEN,
            output=_OUTPUT_GPT6,
            supports_explicit_prompt_cache=True,
        ),
        Model(
            name="gpt-6-sol",
            note="GPT-6 mid-tier",
            context_tokens=1_050_000,
            structured_output=_SCHEMA,
            rates=_gpt6_rates("2.00", "10.00", "0.20", "4.00", "15.00", "0.40"),
            reasoning=_6_GEN,
            output=_OUTPUT_GPT6,
            supports_explicit_prompt_cache=True,
        ),
        Model(
            name="gpt-6-astra",
            note="GPT-6 flagship, highest cost",
            context_tokens=1_050_000,
            structured_output=_SCHEMA,
            rates=_gpt6_rates("10.00", "50.00", "1.00", "20.00", "75.00", "2.00"),
            reasoning=_6_ASTRA,
            output=_OUTPUT_GPT6,
            supports_explicit_prompt_cache=True,
        ),
    ),
)
