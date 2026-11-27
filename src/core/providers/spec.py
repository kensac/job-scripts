"""Shared shapes for provider and model facts.

A provider declares its endpoint, wire protocol, and authentication. Each model
declares its own rates, context, structured output, and reasoning values. An
unknown fact is None, never inferred from another model or treated as zero."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class StructuredOutput(StrEnum):
    """How far a provider can be trusted to return a parseable object."""

    NONE = "none"
    # Valid JSON, but keys and types are not schema-enforced.
    JSON_OBJECT = "json_object"
    # Provider-enforced schema; required by the OpenAI SDK's .parse().
    JSON_SCHEMA = "json_schema"


class Wire(StrEnum):
    """Request/response protocol used to select the client implementation."""

    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"


@dataclass(frozen=True)
class Source:
    """Provenance for a published provider or pricing fact.

    `vendor` is True for primary vendor documentation and False for third-party
    sources.
    """

    url: str
    read_on: datetime.date | None
    vendor: bool
    note: str = ""


@dataclass(frozen=True)
class Tier:
    """Rates for requests whose prompt is at or below `up_to_prompt_tokens`.

    A tier carries all rates because tiered providers bill the whole request at
    the selected tier. `up_to_prompt_tokens=None` is the unbounded final tier.
    """

    up_to_prompt_tokens: int | None
    rate_in: Decimal
    rate_out: Decimal
    # None: no published cached rate; callers bill cache hits at full input rate.
    rate_cached_in: Decimal | None
    # None: no published cache-write rate; reported writes remain unpriced.
    rate_cache_write_in: Decimal | None = None


@dataclass(frozen=True)
class PeakWindow:
    """Undiscounted hours in UTC.

    `isodows` uses ISO 1..7 (Monday..Sunday, matching Postgres EXTRACT(isodow)).
    Hours are half-open: [start_hour_utc, end_hour_utc).
    """

    isodows: tuple[int, ...]
    start_hour_utc: int
    end_hour_utc: int


@dataclass(frozen=True)
class Rates:
    """Published pricing for one model.

    `batch_rate` is None when the model has no batch discount, so batched calls
    bill at synchronous rates.
    """

    tiers: tuple[Tier, ...]
    batch_rate: Decimal | None
    source: Source
    # Separate from `source` when batch pricing comes from a different reference.
    batch_source: Source | None = None
    cache_write_source: Source | None = None

    # Tier rates above are peak rates; `off_peak_multiplier` scales input,
    # output, and cached rates outside `peak_windows`. Unknown call times bill
    # at peak rates rather than assuming a discount.
    off_peak_multiplier: Decimal | None = None
    peak_windows: tuple[PeakWindow, ...] = ()
    off_peak_source: Source | None = None


@dataclass(frozen=True)
class Reasoning:
    """Per-model reasoning control and accepted values.

    Models absent from the registry bypass local validation so unknown models
    are not blocked ahead of the provider.
    """

    param: str | None
    accepts: tuple[str, ...]
    rejects: tuple[str, ...]
    default: str | None
    source: Source


@dataclass(frozen=True)
class Output:
    """Output token limits and truncation behavior.

    `truncates_silently` marks providers that stop mid-JSON without reporting a
    truncation finish reason.
    """

    max_output_tokens: int | None
    default_max_output_tokens: int
    truncation_finish_reason: str | None
    truncates_silently: bool


@dataclass(frozen=True)
class StructuredOutputSpec:
    """Provider-specific requirements for shaping a structured-output request."""

    mode: StructuredOutput
    requires_literal_json_in_prompt: bool = False
    schema_requires_all_fields_required: bool = False
    schema_requires_additional_properties_false: bool = False


@dataclass(frozen=True)
class Model:
    name: str
    # Shown in the UI model picker.
    note: str
    # None means unverified.
    context_tokens: int | None
    structured_output: StructuredOutputSpec
    rates: Rates
    reasoning: Reasoning
    output: Output
    # False hides internal-only models (e.g. embeddings) from the UI picker while keeping pricing.
    selectable: bool = True
    supports_explicit_prompt_cache: bool | None = None
    # Whether this codebase has a batch collector for the model (None = unverified).
    batch_supported: bool | None = None
    cache_write_5m_per_mtok: Decimal | None = None
    cache_write_1h_per_mtok: Decimal | None = None


@dataclass(frozen=True)
class Provider:
    name: str
    wire: Wire
    # None uses the SDK's default endpoint.
    base_url: str | None
    api_key_env: str
    supports_temperature: bool
    batch_endpoint: str | None
    models: tuple[Model, ...]
    source: Source
    # Lowercase error fragments indicating account credit/billing exhaustion
    # (never rate limits, 5xx, or timeouts). Empty means no exhaustion markers.
    exhaustion_markers: tuple[str, ...] = ()
