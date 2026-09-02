"""DeepSeek, as a datasheet.

Every transport fact below was established by a live probe on 2026-09-02, not
read off a page, because DeepSeek is the provider whose documentation and
behaviour disagree most. The rates are the opposite: read off the vendor's own
pricing page and dated. Both provenances are recorded per field.

THE TIER RATES HERE ARE THE PEAK RATES. DeepSeek bills by wall-clock time -
peak 01:00-04:00 and 06:00-10:00 UTC on weekdays, everything else at exactly
half - so 133 of a week's 168 hours are discounted and a scheduled sweep at
03:00 UTC pays double the same work at 12:00 UTC. Peak is the declared base
deliberately: a caller that cannot say when a request happened overstates
rather than inventing a discount, the same direction Tier.rate_cached_in
takes. This is the only provider here where WHEN a call runs changes the bill,
and with no batch lane it is the only discount lever DeepSeek has.
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
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note="peak rates; off-peak is exactly half - see PEAK_WINDOWS",
)

# Transport facts, all from a live probe rather than documentation. The probe
# is the source in the same sense OpenAI's 400 text is: it is the provider
# answering, which beats a page that may describe a different version.
_PROBE = Source(
    url="https://api.deepseek.com/chat/completions",
    read_on=datetime.date(2026, 9, 2),
    vendor=True,
    note="established by live probe; provider error text quoted verbatim below",
)


# isodow, so 1-5 is Monday to Friday, and half-open on the hour: 04:00:00 is
# already off-peak. Both conventions are spec.PeakWindow's, not this file's.
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


# Refused verbatim, on both the raw response_format and the OpenAI SDK's
# .parse() helper:
#
#   "This response_format type is unavailable now"
#
# That second failure is the one that matters. Every structured call in this
# codebase goes through the SDK's strict-schema path, so DeepSeek cannot ride
# the generic openai_compatible route no matter how much of the chat protocol
# it speaks - it needs a json_object branch that validates the shape after the
# fact rather than having the provider enforce it.
#
# And omitting the literal word is its own 400:
#
#   "Prompt must contain the word 'json' in some form to use 'response_format'
#    of type 'json_object'."
_SCHEMA = StructuredOutputSpec(
    mode=StructuredOutput.JSON_OBJECT,
    requires_literal_json_in_prompt=True,
)


# reasoning_effort accepts every value probed - minimal, low, medium, high and
# none - so unlike xAI there is nothing to enumerate by rejection, and unlike
# OpenAI's two generations there is no split.
#
# What the probe DID find is invisible in every response field except the token
# count. effort changes the INPUT size, reproducibly, 3 runs of 3 on an
# identical 11-token prompt:
#
#     unset -> 90    minimal -> 11    low -> 11
#     medium -> 90   high -> 90       none -> 11
#
# DeepSeek injects a ~79-token thinking scaffold for medium, high, and for the
# DEFAULT - so a call that simply does not set reasoning_effort takes the
# expensive branch. Across a 20,730-request sweep that is 1.6M input tokens of
# scaffold nobody asked for. Hence a default of "low": it is the cheapest value
# that still reasons, and leaving this None would mean paying for the scaffold
# by omission.
_REASONING = Reasoning(
    param="reasoning_effort",
    accepts=("minimal", "low", "medium", "high", "none"),
    rejects=(),
    default="low",
    source=_PROBE,
)


# finish_reason='length' with a 200 and the partial content - the ordinary
# Chat Completions signal, present and checkable. Recorded as NOT silent
# against the received wisdom that it is: telling the next person there is no
# signal to check, when there is one, is worse than saying nothing.
_OUTPUT = Output(
    max_output_tokens=384_000,
    default_max_output_tokens=6000,
    truncation_finish_reason="length",
    truncates_silently=False,
)


def _rates(rate_in: str, rate_out: str, rate_cached_in: str) -> Rates:
    return Rates(
        tiers=(Tier(None, Decimal(rate_in), Decimal(rate_out), Decimal(rate_cached_in)),),
        # Probed, not assumed: GET /v1/batches and GET /batches both return
        # 404. There is no batch lane to discount, so a batched call bills at
        # the synchronous rate. The off-peak window is the equivalent lever
        # here, and it is a bigger one.
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
    # Probed: 0.0 and 2.0 accepted, 3.0 refused with "Invalid temperature
    # value, the valid range of temperature is [0, 2]".
    supports_temperature=True,
    batch_endpoint=None,
    source=_PROBE,
    models=(
        # models.list returns exactly these three. deepseek-chat and
        # deepseek-reasoner both still resolve and BOTH serve
        # deepseek-v4-flash, confirmed by the model field on the response - so
        # anyone picking "deepseek-reasoner" for its reasoning gets flash. They
        # are deliberately not declared as models here: a name in the picker
        # that silently serves a different one is the drift this package
        # exists to end. There is no "deepseek-v4" model id; V4 is the
        # generation.
        Model(
            name="deepseek-v4-flash",
            note="Cheapest DeepSeek; also served by the deepseek-chat alias",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("0.44", "1.32", "0.014"),
            reasoning=_REASONING,
            output=_OUTPUT,
        ),
        Model(
            name="deepseek-v4-pro",
            note="Stronger DeepSeek; 3x flash on input and output",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("1.32", "3.96", "0.044"),
            reasoning=_REASONING,
            output=_OUTPUT,
        ),
        Model(
            name="deepseek-v4-flash-vision-exp",
            note="Experimental vision variant; priced identically to flash",
            context_tokens=1_000_000,
            structured_output=_SCHEMA,
            rates=_rates("0.44", "1.32", "0.014"),
            reasoning=_REASONING,
            output=_OUTPUT,
        ),
    ),
)
