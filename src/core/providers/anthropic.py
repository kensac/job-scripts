"""Anthropic, as a datasheet.

REVIEW NOTE. As with OpenAI, every rate here is UNSOURCED - carried over from
the table this package replaced, which recorded no provenance. Marked
vendor=False with no read date so the gap shows up in review rather than
passing as verified.

There is no production traffic to check them against either: at the time of
writing, `ai_queries` and `api_usage` both hold zero rows with a claude model.
So nothing below has ever been exercised, which is worth knowing before
trusting any of it.
"""

from __future__ import annotations

import datetime
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

_UNSOURCED = Source(
    url="",
    read_on=None,
    vendor=False,
    note="carried over from the original price table, which recorded no source",
)

_BATCH = Source(
    url="https://www.digitalapplied.com/blog/llm-batch-api-pricing-landscape-2026",
    read_on=datetime.date(2026, 9, 2),
    vendor=False,
    note="article dated 2026-08-14; Message Batches at 50%",
)

# One accepted set across the current models, unlike OpenAI's split. Taken from
# the constant this package replaced rather than from a vendor page, so it is
# marked unverified - and it has never been exercised, since no Anthropic call
# has ever run here.
_EFFORT = Reasoning(
    param="effort",
    accepts=("low", "medium", "high", "xhigh", "max"),
    rejects=(),
    default=None,
    source=Source(
        url="",
        read_on=None,
        vendor=False,
        note="carried over from _EFFORTS_ANTHROPIC; never exercised against the API",
    ),
)

_OUTPUT = Output(
    max_output_tokens=None,
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
        source=_UNSOURCED,
        batch_source=_BATCH,
    )


# CACHE WRITES ARE NOT MODELLED, deliberately. Anthropic bills cache CREATION at
# a premium over its input rate and cache READS at a discount, and only the
# discount is represented above. That is correct only because nothing in this
# codebase ever creates a cache entry: no call site sets cache_control, so the
# only cached tokens we are ever billed for are reads. If explicit prompt
# caching is ever switched on, this file is wrong until a cache-write rate is
# added to Tier - and it will be wrong in the expensive direction, understating
# the bill. The same applies to qwen3.8-max, which publishes a 125% creation
# rate.
PROVIDER = Provider(
    name="anthropic",
    wire=Wire.ANTHROPIC_MESSAGES,
    base_url=None,
    api_key_env="ANTHROPIC_API_KEY",
    supports_temperature=False,
    batch_endpoint=None,
    source=Source(
        url="",
        read_on=None,
        vendor=False,
        note="transport facts from this codebase's integration; never exercised in production",
    ),
    models=(
        Model(
            name="claude-opus-5",
            note="Most capable, best default for Anthropic keys",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("5.00", "25.00", "0.500"),
            reasoning=_EFFORT,
            output=_OUTPUT,
        ),
        Model(
            name="claude-sonnet-5",
            # Sonnet 5's launch intro price ($2/$10) became the standard price.
            note="Strong quality at lower cost",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("2.00", "10.00", "0.200"),
            reasoning=_EFFORT,
            output=_OUTPUT,
        ),
        Model(
            name="claude-haiku-4-5",
            note="Fast and cheap for simple filters",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("1.00", "5.00", "0.100"),
            reasoning=_EFFORT,
            output=_OUTPUT,
        ),
    ),
)
