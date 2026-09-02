"""The standard format every provider module fills in.

This file holds no facts about any provider - only the shape they are all
declared in. That separation is the point: the router reads the same fields
from every provider, and a provider that behaves differently says so in a
field rather than in a branch somewhere downstream.

Two levels, because the facts genuinely live at two levels. A provider owns its
endpoint, wire protocol and auth. A MODEL owns its rates, its context, what
structured output it can do and which reasoning values it accepts - because
those vary within a provider. xAI discounts four of its seven models for batch
and charges the other three full price; Alibaba supports strict schemas on its
3.7-and-later models and not on Turbo. Declaring either at provider level is
how `_EFFORTS_OPENAI` became a union of every generation's accepted values.

Nothing here is inferred. A field whose answer is unknown is None, and None
means "nobody has looked it up", never "zero" and never "same as the other one".
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class StructuredOutput(StrEnum):
    """How far a provider can be trusted to return a parseable object."""

    NONE = "none"
    # Valid JSON, but the shape is not enforced - the model may return
    # different keys than asked for.
    JSON_OBJECT = "json_object"
    # A schema the provider enforces. This is what the OpenAI SDK's .parse()
    # sends, so it is the only mode that path can use.
    JSON_SCHEMA = "json_schema"


class Wire(StrEnum):
    """Which request/response protocol the provider speaks.

    Sharing a wire format is not sharing a contract: xAI speaks OpenAI's chat
    protocol and still has its own rates, its own batch eligibility and its own
    schema rules. Wire decides which client code runs, and nothing else.
    """

    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"


@dataclass(frozen=True)
class Source:
    """Where a number came from and when someone last looked.

    Kanishk reviews these roughly monthly, which only works if every number can
    be re-checked without archaeology. `vendor` separates a rate read off the
    provider's own page from one taken second-hand - while this table was being
    built the aggregators contradicted both the vendors and each other, on
    DeepSeek's cache rate, on Qwen's tiering and on Alibaba's regional spread.
    """

    url: str
    read_on: datetime.date | None
    vendor: bool
    note: str = ""


@dataclass(frozen=True)
class Tier:
    """Rates that apply when a request's prompt is at or below `up_to_prompt_tokens`.

    A tier carries all three rates rather than just the input one, because the
    providers that tier bill the WHOLE request at the tier its prompt selects.
    `up_to_prompt_tokens=None` is the last tier and has no ceiling.
    """

    up_to_prompt_tokens: int | None
    rate_in: Decimal
    rate_out: Decimal
    # None means the vendor does not publish a cached rate for this model, not
    # that caching is free. Callers bill it at the full input rate: that
    # overstates a cache hit rather than inventing a discount.
    rate_cached_in: Decimal | None


@dataclass(frozen=True)
class Rates:
    """What one model costs, as published.

    `batch_rate` is None when the vendor runs no batch lane for this model, in
    which case a batched call bills at the synchronous rate. It is per-model
    and not global: a single 0.5 multiplier would report half the true cost of
    a batched call on a model its vendor never discounted.
    """

    tiers: tuple[Tier, ...]
    batch_rate: Decimal | None
    source: Source
    # Separate provenance, because a vendor that publishes its rates plainly
    # may say nothing about its batch lane. xAI is the case that forced this:
    # its per-token rates are on its own model page and its 20% batch discount
    # appears only second-hand. None means no batch lane is claimed at all.
    batch_source: Source | None = None


@dataclass(frozen=True)
class Reasoning:
    """Which thinking control a model takes, and what it will actually accept.

    Declared per model, because the accepted set moves between generations -
    the newer OpenAI models reject "minimal" with a 400 while the older ones
    require it, and a union covering both accepts values that half the catalogue
    refuses.

    A model absent from the registry is NOT validated against anything. That is
    deliberate: a wrong rejection blocks a model that works and reads as a
    mystery outage, while a wrong acceptance comes back as the provider's own
    error naming what it supports. Being stricter than the vendor only makes us
    slower than the vendor.
    """

    param: str | None
    accepts: tuple[str, ...]
    rejects: tuple[str, ...]
    default: str | None
    source: Source


@dataclass(frozen=True)
class Output:
    """Output limits, and what failure looks like when they are hit.

    `truncates_silently` is the field worth reading: a provider that stops
    mid-JSON and reports success produces a parse error far from its cause.
    """

    max_output_tokens: int | None
    default_max_output_tokens: int
    truncation_finish_reason: str | None
    truncates_silently: bool


@dataclass(frozen=True)
class StructuredOutputSpec:
    """How a provider wants a structured request shaped.

    Every flag here answers that one question, which is what keeps it from
    becoming a drawer for whatever a vendor does oddly. DeepSeek requiring the
    literal word "json" in the prompt is a schema-request-shaping fact and
    belongs here; something that is not would need its own home.
    """

    mode: StructuredOutput
    requires_literal_json_in_prompt: bool = False
    schema_requires_all_fields_required: bool = False
    schema_requires_additional_properties_false: bool = False


@dataclass(frozen=True)
class Model:
    name: str
    # Shown in the UI model picker.
    note: str
    # None means nobody has looked it up yet, and it shows as a gap in review.
    context_tokens: int | None
    structured_output: StructuredOutputSpec
    rates: Rates
    reasoning: Reasoning
    output: Output
    # False for a model that is used internally but must never appear in the
    # user's model picker - an embedding model has no chat interface to offer.
    # It is still priced: being unofferable and being unpriced are different
    # things, and conflating them is how a real cost becomes invisible.
    selectable: bool = True


@dataclass(frozen=True)
class Provider:
    name: str
    wire: Wire
    # None means the SDK's own default endpoint.
    base_url: str | None
    api_key_env: str
    supports_temperature: bool
    batch_endpoint: str | None
    models: tuple[Model, ...]
    source: Source
