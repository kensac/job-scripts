"""DeepSeek provider datasheet.

Transport behavior comes from live probes (2026-09-02); rates come from the
vendor pricing page. Tier rates are peak rates (weekday 01:00-04:00 and
06:00-10:00 UTC); off-peak hours bill at 0.5x so unknown timestamps default
conservatively to peak pricing.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from core.providers.spec import (
    Model,
    Output,
    PeakWindow,
    Provider,
    Rates,
    Reasoning,
    Source,
    StructuredOutput,
    StructuredOutputSpec,
    Tier,
    Wire,
)

_PRICING = Source(
    url="https://api-docs.deepseek.com/quick_start/pricing",
    read_on=datetime.date(2026, 9, 23),
    vendor=True,
    note="peak rates; off-peak is exactly half - see PEAK_WINDOWS",
)

_PROBE = Source(
    url="https://api.deepseek.com/chat/completions",
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note="established by live probe; provider error text quoted verbatim below",
)


# isodow 1-5 (Mon-Fri), half-open UTC hours per spec.PeakWindow.
_WEEKDAYS = (1, 2, 3, 4, 5)
_PEAK_WINDOWS = (
    PeakWindow(isodows=_WEEKDAYS, start_hour_utc=1, end_hour_utc=4),
    PeakWindow(isodows=_WEEKDAYS, start_hour_utc=6, end_hour_utc=10),
)
_OFF_PEAK = Source(
    url="https://api-docs.deepseek.com/quick_start/pricing",
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note=(
        "'Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 "
        "and 06:00 - 10:00 UTC, Monday through Friday (all other hours are "
        "off-peak).' Quoted from the vendor page."
    ),
)


# Probed 2026-09-02: json_schema is refused ("This response_format type is
# unavailable now"), and json_object requires the literal word "json" in the prompt.
_SCHEMA = StructuredOutputSpec(
    mode=StructuredOutput.JSON_OBJECT,
    requires_literal_json_in_prompt=True,
)


# Probed 2026-09-02: omitting reasoning_effort (or passing medium/high) injects
# a ~79-token thinking scaffold into input tokens; default to "low" to avoid it.
_REASONING = Reasoning(
    param="reasoning_effort",
    accepts=("minimal", "low", "medium", "high", "none"),
    rejects=(),
    default="low",
    source=_PROBE,
)


_OUTPUT = Output(
    max_output_tokens=384_000,
    default_max_output_tokens=6000,
    truncation_finish_reason="length",
    truncates_silently=False,
)


def _rates(rate_in: str, rate_out: str, rate_cached_in: str) -> Rates:
    return Rates(
        tiers=(Tier(None, Decimal(rate_in), Decimal(rate_out), Decimal(rate_cached_in)),),
        # Probed: /v1/batches and /batches return 404.
        batch_rate=None,
        source=_PRICING,
        batch_source=None,
        off_peak_multiplier=Decimal("0.5"),
        peak_windows=_PEAK_WINDOWS,
        off_peak_source=_OFF_PEAK,
    )


PROVIDER = Provider(
    name="deepseek",
    wire=Wire.OPENAI_CHAT,
    base_url="https://api.deepseek.com",
    api_key_env="DEEPSEEK_API_KEY",
    # Probed valid range: [0, 2].
    supports_temperature=True,
    batch_endpoint=None,
    # HTTP 402 "Insufficient Balance".
    exhaustion_markers=("insufficient balance",),
    source=_PROBE,
    models=(
        # deepseek-chat and deepseek-reasoner are omitted because both resolve to
        # deepseek-v4-flash on the wire.
        Model(
            name="deepseek-v4-flash",
            note="Legacy alias now served and billed as DeepSeek V4.1 Flash",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("0.30", "1.20", "0.006"),
            reasoning=_REASONING,
            output=_OUTPUT,
        ),
        Model(
            name="deepseek-v4-pro",
            note="DeepSeek V4 Pro 0813; off-peak pricing available",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("1.32", "3.96", "0.044"),
            reasoning=_REASONING,
            output=_OUTPUT,
        ),
        Model(
            name="deepseek-v4-flash-vision-exp",
            note="Legacy vision alias now served and billed as DeepSeek V4.1 Flash",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("0.30", "1.20", "0.006"),
            reasoning=_REASONING,
            output=_OUTPUT,
        ),
        Model(
            name="deepseek-flash",
            note="Current DeepSeek V4.1 Flash; text and vision, off-peak pricing",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("0.30", "1.20", "0.006"),
            reasoning=_REASONING,
            output=_OUTPUT,
            batch_supported=False,
        ),
    ),
)
