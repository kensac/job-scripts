"""xAI, as a datasheet.

xAI speaks OpenAI's chat protocol, which is why it needs no client of its own -
only a base_url. That is also the whole argument for this package: sharing a
wire format is not sharing a contract. Every commercially interesting fact
about xAI differs from OpenAI's - it tiers by prompt length, its cached rate is
25% of input rather than 10%, its models disagree with each other about which
reasoning values they accept, and it takes a temperature parameter OpenAI's
Responses API does not.

Rates read 2026-09-02 off the vendor's own model page. Reasoning sets were
probed live the same day, because unlike OpenAI, xAI does NOT name the
supported set when it rejects one - a bad value returns only
{'code': 'invalid-argument', 'error': 'Invalid reasoning effort.'} - so each
value had to be tried individually.
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

_DOCS = Source(
    url="https://docs.x.ai/docs/models",
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note="vendor model page; the page itself carries no last-updated date",
)

_PROBED = Source(
    url="https://api.x.ai/v1/chat/completions",
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note=(
        "enumerated by trying each value live; xAI's 400 does not name the "
        "supported set the way OpenAI's does"
    ),
)

# Above this prompt length xAI rebills the WHOLE request - input, cached input
# and output alike - at the higher tier. Both tiers are written out per model
# rather than expressed as "twice the base": that the high tier is currently
# exactly 2x is a fact about today's price list, not a rule xAI has committed
# to, and encoding it as arithmetic would silently absorb a future change.
_TIER_TOKENS = 200_000

# json_schema, confirmed live: client.chat.completions.parse() with a Pydantic
# model returns a parsed object against api.x.ai/v1. Both flags are False and
# that is not an oversight - xAI's schema rules are LOOSER than OpenAI's.
# additionalProperties already defaults to false, and fields omitted from
# `required` are treated as optional rather than rejected.
_SCHEMA = StructuredOutputSpec(mode=StructuredOutput.JSON_SCHEMA)


def _rates(
    lo_in: str, lo_out: str, lo_cached: str, hi_in: str, hi_out: str, hi_cached: str
) -> Rates:
    return Rates(
        tiers=(
            Tier(_TIER_TOKENS, Decimal(lo_in), Decimal(lo_out), Decimal(lo_cached)),
            Tier(None, Decimal(hi_in), Decimal(hi_out), Decimal(hi_cached)),
        ),
        # NO BATCH RATE, deliberately, even though the lane exists: GET
        # /v1/batches returns 200 with an empty list, so xAI does run one.
        #
        # What is not established is the DISCOUNT. One aggregator
        # (digitalapplied.com, 2026-08-14) reports 20% off for exactly four
        # legacy models - grok-4.3 and the three grok-4.20 builds - with the
        # flagship excluded, and no xAI page found states any figure. The
        # discount is not discoverable over the wire either; a batch has to be
        # run and billed to learn it.
        #
        # None means a batched call bills at the synchronous rate, which
        # OVERSTATES if the 20% is real. That is the safe direction and the
        # same rule as an unpublished cached rate: overstate rather than invent
        # a discount. A global 0.5 here would have reported half.
        batch_rate=None,
        source=_DOCS,
    )


def _output() -> Output:
    return Output(
        max_output_tokens=None,
        default_max_output_tokens=6000,
        truncation_finish_reason="length",
        truncates_silently=False,
    )


# grok-4.5 and grok-4.6 accept the same set; grok-4.3 additionally accepts
# "none". All three reject "max" despite it being valid on OpenAI's 5.6 family,
# which is why this is declared per model and never shared across a provider.
_EFFORT_4_3 = Reasoning(
    param="reasoning_effort",
    accepts=("none", "minimal", "low", "medium", "high", "xhigh"),
    rejects=("max",),
    default="low",
    source=_PROBED,
)

_EFFORT_4_5_AND_4_6 = Reasoning(
    param="reasoning_effort",
    accepts=("minimal", "low", "medium", "high", "xhigh"),
    rejects=("none", "max"),
    default="low",
    source=_PROBED,
)

# Only the three models this system would actually offer are declared. xAI also
# publishes grok-build-0.1 and three grok-4.20 builds; their rates are on the
# vendor page above, but declaring a model means asserting its reasoning set,
# and those four were never probed. An undeclared model is simply unavailable,
# which is better than a declared one whose accepted values are a guess.
PROVIDER = Provider(
    name="xai",
    wire=Wire.OPENAI_CHAT,
    base_url="https://api.x.ai/v1",
    api_key_env="XAI_API_KEY",
    # Confirmed live: temperature=0.5 is accepted, unlike OpenAI's Responses API.
    supports_temperature=True,
    batch_endpoint="/v1/batches",
    source=_DOCS,
    models=(
        Model(
            name="grok-4.3",
            note="Cheapest Grok; 1M context",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("1.25", "2.50", "0.20", "2.50", "5.00", "0.40"),
            reasoning=_EFFORT_4_3,
            output=_output(),
        ),
        Model(
            name="grok-4.5",
            note="Stronger; 500K context",
            context_tokens=500_000,
            structured_output=_SCHEMA,
            rates=_rates("2.00", "6.00", "0.30", "4.00", "12.00", "0.60"),
            reasoning=_EFFORT_4_5_AND_4_6,
            output=_output(),
        ),
        Model(
            name="grok-4.6",
            note="Newest flagship, highest cost; 500K context",
            context_tokens=500_000,
            # 25% of input, against OpenAI's 10%. The single global
            # CACHED_INPUT_MULTIPLIER this package replaced would have
            # understated every cached grok-4.6 token by a factor of 2.5.
            structured_output=_SCHEMA,
            rates=_rates("2.00", "6.00", "0.50", "4.00", "12.00", "1.00"),
            reasoning=_EFFORT_4_5_AND_4_6,
            output=_output(),
        ),
    ),
)
