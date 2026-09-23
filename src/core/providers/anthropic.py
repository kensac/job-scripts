"""Direct Claude API rates and capabilities, reviewed 2026-09-23."""

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
    url="https://platform.claude.com/docs/en/about-claude/pricing",
    read_on=datetime.date(2026, 9, 23),
    vendor=True,
    note="Direct global API prices; cache and batch discounts stack",
)

_BATCH = Source(
    url="https://platform.claude.com/docs/en/build-with-claude/batch-processing",
    read_on=datetime.date(2026, 9, 23),
    vendor=True,
    note="Message Batches at 50%; requires a separate collector",
)

# Haiku has no effort control. The other listed current models support these
# levels; preserving an unset default avoids changing existing request intent.
_EFFORT = Reasoning(
    param="effort",
    accepts=("low", "medium", "high", "xhigh", "max"),
    rejects=(),
    default=None,
    source=Source(
        url="https://platform.claude.com/docs/en/build-with-claude/effort",
        read_on=datetime.date(2026, 9, 23),
        vendor=True,
    ),
)

_OUTPUT = Output(
    max_output_tokens=128_000,
    default_max_output_tokens=4000,
    truncation_finish_reason="max_tokens",
    truncates_silently=False,
)

# The Messages API takes a Pydantic model as output_format and enforces it.
_SCHEMA = StructuredOutputSpec(mode=StructuredOutput.JSON_SCHEMA)


def _rates(rate_in: str, rate_out: str, rate_cached_in: str) -> Rates:
    return Rates(
        tiers=(Tier(None, Decimal(rate_in), Decimal(rate_out), Decimal(rate_cached_in)),),
        batch_rate=Decimal("0.5"),
        source=_PRICES,
        batch_source=_BATCH,
    )


_MODELS = Source(
    "https://platform.claude.com/docs/en/models/overview",
    datetime.date(2026, 9, 23),
    True,
)
_HAIKU_EFFORT = Reasoning(None, (), (), None, _MODELS)
_HAIKU_OUTPUT = Output(64_000, 4000, "max_tokens", False)
_API_MODELS = Source(
    "https://api.anthropic.com/v1/models",
    datetime.date(2026, 9, 23),
    True,
    "Read-only model catalog: limits, effort levels and structured-output support",
)
_EFFORT_46 = Reasoning("effort", ("low", "medium", "high", "max"), ("xhigh",), None, _API_MODELS)
_EFFORT_45 = Reasoning("effort", ("low", "medium", "high"), ("xhigh", "max"), None, _API_MODELS)


# Cache creation is not requested by this integration. Do not treat one-hour
# cache writes as five-minute writes if explicit caching is added later.
def _current(name: str, note: str, rate_in: str, rate_out: str, cached: str) -> Model:
    return Model(
        name=name,
        note=note,
        context_tokens=1_000_000,
        structured_output=_SCHEMA,
        rates=_rates(rate_in, rate_out, cached),
        reasoning=_EFFORT,
        output=_OUTPUT,
        batch_supported=True,
        cache_write_5m_per_mtok=Decimal(rate_in) * Decimal("1.25"),
        cache_write_1h_per_mtok=Decimal(rate_in) * Decimal("2"),
    )


PROVIDER = Provider(
    name="anthropic",
    wire=Wire.ANTHROPIC_MESSAGES,
    base_url=None,
    api_key_env="ANTHROPIC_API_KEY",
    supports_temperature=False,
    batch_endpoint="/v1/messages/batches",
    source=_MODELS,
    models=(
        Model(
            name="claude-opus-5",
            note="Previous Opus generation; existing default preserved",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("5.00", "25.00", "0.500"),
            reasoning=_EFFORT,
            output=_OUTPUT,
            batch_supported=True,
            cache_write_5m_per_mtok=Decimal("6.25"),
            cache_write_1h_per_mtok=Decimal("10"),
        ),
        Model(
            name="claude-sonnet-5",
            # Sonnet 5's launch intro price ($2/$10) became the standard price.
            note="Strong quality at lower cost",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("2.00", "10.00", "0.200"),
            reasoning=_EFFORT,
            output=_OUTPUT,
            batch_supported=True,
            cache_write_5m_per_mtok=Decimal("2.5"),
            cache_write_1h_per_mtok=Decimal("4"),
        ),
        Model(
            name="claude-haiku-4-5",
            note="Fast and cheap for simple filters",
            context_tokens=200_000,
            structured_output=_SCHEMA,
            rates=_rates("1.00", "5.00", "0.100"),
            reasoning=_HAIKU_EFFORT,
            output=_HAIKU_OUTPUT,
            batch_supported=True,
            cache_write_5m_per_mtok=Decimal("1.25"),
            cache_write_1h_per_mtok=Decimal("2"),
        ),
        _current("claude-opus-5-5", "Current Opus; adaptive thinking always on", "4", "20", "0.2"),
        _current(
            "claude-fable-5-1",
            "Frontier reasoning; adaptive thinking always on",
            "10",
            "50",
            "0.25",
        ),
        _current("claude-fable-5", "Previous Fable generation", "10", "50", "1"),
        _current("claude-opus-4-8", "Legacy Opus 4.8; 1M context", "5", "25", "0.5"),
        _current("claude-opus-4-7", "Legacy Opus 4.7; 1M context", "5", "25", "0.5"),
        replace(
            _current("claude-opus-4-6", "Legacy Opus 4.6; 1M context", "5", "25", "0.5"),
            reasoning=_EFFORT_46,
        ),
        replace(
            _current("claude-sonnet-4-6", "Legacy Sonnet 4.6; 1M context", "3", "15", "0.3"),
            reasoning=_EFFORT_46,
        ),
        replace(
            _current("claude-opus-4-5-20251101", "Pinned legacy Opus 4.5", "5", "25", "0.5"),
            context_tokens=200_000,
            output=_HAIKU_OUTPUT,
            reasoning=_EFFORT_45,
        ),
        replace(
            _current(
                "claude-sonnet-4-5-20250929",
                "Pinned legacy Sonnet 4.5; standard 200K context",
                "3",
                "15",
                "0.3",
            ),
            # The model API advertises a 1M beta capacity. This client does
            # not send the long-context beta header, so expose the documented
            # standard limit rather than promise a capability we do not use.
            context_tokens=200_000,
            output=_HAIKU_OUTPUT,
            reasoning=_HAIKU_EFFORT,
        ),
        Model(
            name="claude-haiku-4-5-20251001",
            note="Pinned Haiku 4.5 snapshot; no effort parameter",
            context_tokens=200_000,
            structured_output=_SCHEMA,
            rates=_rates("1", "5", "0.1"),
            reasoning=_HAIKU_EFFORT,
            output=_HAIKU_OUTPUT,
            batch_supported=True,
            cache_write_5m_per_mtok=Decimal("1.25"),
            cache_write_1h_per_mtok=Decimal("2"),
        ),
    ),
)
