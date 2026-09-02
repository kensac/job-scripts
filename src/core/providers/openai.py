"""OpenAI, as a datasheet.

REVIEW NOTE. Every rate below is UNSOURCED. They were carried over verbatim
from the table this package replaced, which recorded no provenance, and rather
than attach a URL after the fact they are marked vendor=False with no read date
so the gap is visible. This is the first thing to fix at the next review: read
them off OpenAI's own pricing page and date them.

The reasoning values, by contrast, are empirical and dated - they come from
this system's own production traffic, including a 400 that took down a batch.
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

# The batch lane has always been assumed to halve the bill, and the assumption
# has held. It is second-hand rather than read off OpenAI's own page, so it is
# marked as such.
_BATCH = Source(
    url="https://www.digitalapplied.com/blog/llm-batch-api-pricing-landscape-2026",
    read_on=datetime.date(2026, 9, 2),
    vendor=False,
    note="article dated 2026-08-14; agrees with the multiplier this code has always used",
)

# Read straight out of OpenAI's own 400s on 2026-09-02, which name the
# supported set verbatim:
#
#   gpt-5-mini    "Unsupported value: 'none' is not supported with the
#                  'gpt-5-mini' model. Supported values are: 'minimal', 'low',
#                  'medium', and 'high'."
#   gpt-5.6-luna  "Unsupported value: 'minimal' is not supported with the
#                  'gpt-5.6-luna' model. Supported values are: 'none', 'low',
#                  'medium', 'high', 'xhigh', and 'max'."
#
# So these are vendor-confirmed even though no documentation page carries them:
# the API itself is the source. Note the two generations share only low, medium
# and high - any single effort value used across both must be one of those
# three, which is not a constraint a union of the two sets can express.
_EFFORTS_API = Source(
    url="https://api.openai.com/v1/responses",
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note="quoted from the provider's own 400 error text; see comment above",
)

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

# Covers reasoning AND output on the Responses API; too small and the JSON gets
# truncated mid-string after a long reasoning pass.
_OUTPUT = Output(
    max_output_tokens=None,
    default_max_output_tokens=6000,
    truncation_finish_reason="length",
    truncates_silently=False,
)

_SCHEMA = StructuredOutputSpec(
    mode=StructuredOutput.JSON_SCHEMA,
    schema_requires_all_fields_required=True,
    schema_requires_additional_properties_false=True,
)


def _rates(rate_in: str, rate_out: str, rate_cached_in: str) -> Rates:
    return Rates(
        tiers=(Tier(None, Decimal(rate_in), Decimal(rate_out), Decimal(rate_cached_in)),),
        batch_rate=Decimal("0.5"),
        source=_UNSOURCED,
        batch_source=_BATCH,
    )


PROVIDER = Provider(
    name="openai",
    wire=Wire.OPENAI_RESPONSES,
    base_url=None,
    api_key_env="OPENAI_API_KEY",
    supports_temperature=False,
    batch_endpoint="/v1/responses",
    source=Source(
        url="",
        read_on=None,
        vendor=False,
        note="transport facts are from this codebase's own working integration",
    ),
    models=(
        Model(
            name="gpt-5-nano",
            note="Cheapest, used by default; fine for most filters",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("0.05", "0.40", "0.005"),
            reasoning=_EARLIER_GEN,
            output=_OUTPUT,
        ),
        Model(
            name="gpt-5-mini",
            note="Better judgment on nuanced criteria",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("0.25", "2.00", "0.025"),
            reasoning=_EARLIER_GEN,
            output=_OUTPUT,
        ),
        Model(
            name="gpt-5",
            note="Strong general model",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("1.25", "10.00", "0.125"),
            reasoning=_EARLIER_GEN,
            output=_OUTPUT,
        ),
        Model(
            name="gpt-5.6-luna",
            note="Newest small model, fast and cheap",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("0.20", "1.20", "0.020"),
            reasoning=_5_6_GEN,
            output=_OUTPUT,
        ),
        Model(
            name="gpt-5.6-terra",
            note="Newest mid-tier, strong quality",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("2.00", "12.00", "0.200"),
            reasoning=_5_6_GEN,
            output=_OUTPUT,
        ),
        # Not selectable: used by core/embeddings.py, never offered in the
        # picker. Priced because it costs money - the output rate is a real
        # 0.00 rather than an omission, since an embedding call returns no
        # completion tokens at all.
        Model(
            name="text-embedding-3-small",
            note="Embeddings; not a chat model",
            context_tokens=None,
            structured_output=StructuredOutputSpec(mode=StructuredOutput.NONE),
            rates=_rates("0.02", "0.00", "0.02"),
            reasoning=Reasoning(
                param=None, accepts=(), rejects=(), default=None, source=_UNSOURCED
            ),
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
            note="Newest flagship, highest cost",
            context_tokens=None,
            structured_output=_SCHEMA,
            rates=_rates("4.00", "20.00", "0.400"),
            reasoning=_5_6_GEN,
            output=_OUTPUT,
        ),
    ),
)
