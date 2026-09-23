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

# gpt-6. Unlike everything above it, these rates were read off OpenAI's own
# page rather than inherited, so they carry a URL and a date.
_GPT6_PRICES = Source(
    url="https://developers.openai.com/api/docs/pricing",
    read_on=datetime.date(2026, 9, 22),
    vendor=True,
    note="short- and long-context rows for gpt-6-luna, gpt-6-sol and gpt-6-astra",
)

# "Prompts with more than 272K input tokens are priced at 2x input and cache
# rates and 1.5x output for the full request" - each model's page, read
# 2026-09-22. The whole request rebills at the tier its prompt selects, which
# is what Tier already means.
#
# Both tiers are written out per model rather than as "twice the base": that
# the multipliers are currently 2x and 1.5x is a fact about today's price
# list, not a rule OpenAI has committed to, and arithmetic would absorb a
# future change silently. Same reasoning as xAI's tiers.
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

# Covers reasoning AND output on the Responses API; too small and the JSON gets
# truncated mid-string after a long reasoning pass.
# gpt-6, read the same way on 2026-09-22, and the two answers differ per
# model - astra takes neither "none" nor "minimal", the other two take "none"
# but not "minimal":
#
#   gpt-6-astra  "Unsupported value: 'none' is not supported with the
#                 'gpt-6-astra' model. Supported values are: 'low', 'medium',
#                 'high', 'xhigh', and 'max'."
#   gpt-6-sol    "Unsupported value: 'minimal' is not supported with the
#                 'gpt-6-sol' model. Supported values are: 'none', 'low',
#                 'medium', 'high', 'xhigh', and 'max'."
#
# WORTH KNOWING FOR THE NEXT READER: an invalid value gets a different 400.
# Sending effort="__nonsense__" returns "Invalid value ... Supported values
# are: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'" for all
# three models - that is the PARAMETER's enum, not the model's. Only a real
# value the model refuses produces the per-model list above, and only that
# form is evidence. The first probe here said all three took "minimal"; they
# do not.
_EFFORTS_API_GPT6 = Source(
    url="https://api.openai.com/v1/responses",
    read_on=datetime.date(2026, 9, 22),
    vendor=True,
    note="per-model 400 text; an invalid value returns the parameter enum instead",
)

# Default stays "low" like every other OpenAI entry here - the value the
# datasheet says is safe when a task expresses no preference. OpenAI's own
# default for this generation is "medium", which is the more expensive half of
# a choice this system already made deliberately.
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

_OUTPUT = Output(
    max_output_tokens=128_000,
    default_max_output_tokens=6000,
    truncation_finish_reason="length",
    truncates_silently=False,
)

# gpt-6 publishes both numbers, so they are facts here rather than blanks:
# "1,050,000 context window", "128,000 max output tokens".
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
    """Two tiers, the second selected by a prompt over _GPT6_TIER_TOKENS.

    Cache writes follow the same 1.25x of uncached input as the 5.6
    generation: the prompt-caching guide says "GPT-5.6 and later", and the
    read rates on the pricing page are the 0.1x that sentence also states,
    so the write half of it is taken to hold too.
    """
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
        # gpt-6, cheapest first, which is the order the picker offers them in.
        # Every number below is off OpenAI's own pages on 2026-09-22; the
        # batch lane is stated there too ("a 50% discount across all three
        # models"), so unlike the models above it this generation's batch rate
        # is vendor-sourced rather than inherited from an aggregator.
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
