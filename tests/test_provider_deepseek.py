"""DeepSeek: wall-clock pricing, and a provider that will not enforce a schema.

Everything asserted here was established by live probe on 2026-09-02 or read
off the vendor's dated pricing page. The point of the tests is that neither can
rot silently: a rate that loses its source, a window that stops being half-open,
or a parse path that starts trusting the provider's output, all fail here.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from api import ai, db
from core import pricing, providers
from core.providers.spec import StructuredOutput

FLASH = "deepseek-v4-flash"
PRO = "deepseek-v4-pro"

# 2026-09-07 is a Monday, 2026-09-05 a Saturday. Fixed dates, because a test
# that computes "next Monday" is a test that fails on a different day.
MON = datetime.date(2026, 9, 7)
SAT = datetime.date(2026, 9, 5)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=datetime.UTC)


class TestDatasheet:
    def test_the_provider_is_registered(self):
        assert "deepseek" in providers.PROVIDERS
        assert providers.provider_of(FLASH) == "deepseek"

    def test_rates_are_vendor_sourced_and_dated(self):
        """The rule that makes a monthly review possible: an undated number can
        only be re-guessed."""
        for m in providers.PROVIDERS["deepseek"].models:
            source = m.rates.source
            assert source.vendor, f"{m.name} rates are not vendor-sourced"
            assert source.read_on == datetime.date(2026, 9, 2)
            assert source.url

    def test_the_off_peak_discount_carries_its_own_provenance(self):
        for m in providers.PROVIDERS["deepseek"].models:
            assert m.rates.off_peak_multiplier is not None
            assert m.rates.off_peak_source is not None
            assert m.rates.off_peak_source.vendor

    def test_no_batch_lane_is_claimed(self):
        """Probed, not assumed: GET /v1/batches and /batches both 404. A
        batch_rate here would report half the true cost of every batched call."""
        assert providers.PROVIDERS["deepseek"].batch_endpoint is None
        for m in providers.PROVIDERS["deepseek"].models:
            assert m.rates.batch_rate is None

    def test_a_batched_call_bills_at_the_synchronous_rate(self):
        peak = _at(MON, 3)
        assert pricing.estimate_cost_usd(FLASH, 1_000_000, 0, batched=True, at=peak) == (
            pricing.estimate_cost_usd(FLASH, 1_000_000, 0, at=peak)
        )

    def test_the_aliases_are_not_declared_as_models(self):
        """deepseek-chat and deepseek-reasoner both resolve, and both serve
        deepseek-v4-flash. Declaring either would put a name in the picker that
        silently serves a different model - someone choosing 'reasoner' for a
        hard filter would get flash, cheaper and quieter, with no error."""
        assert providers.model("deepseek-chat") is None
        assert providers.model("deepseek-reasoner") is None

    def test_structured_output_is_json_object_not_schema(self):
        """Probed: json_schema is refused with 'This response_format type is
        unavailable now', including through the SDK's .parse() helper."""
        for m in providers.PROVIDERS["deepseek"].models:
            assert m.structured_output.mode is StructuredOutput.JSON_OBJECT
            assert m.structured_output.requires_literal_json_in_prompt

    def test_the_reasoning_default_is_one_of_the_cheap_values(self):
        """Not cosmetic. Probed 3 of 3 on an identical 11-token prompt: unset,
        medium and high cost 90 prompt tokens; minimal, low and none cost 11.
        A None default here would pay a 79-token scaffold on every call."""
        for m in providers.PROVIDERS["deepseek"].models:
            assert m.reasoning.default in ("minimal", "low", "none")

    @pytest.mark.parametrize(
        ("model", "rate_in", "rate_out", "rate_cached"),
        [
            (FLASH, "0.44", "1.32", "0.014"),
            (PRO, "1.32", "3.96", "0.044"),
            ("deepseek-v4-flash-vision-exp", "0.44", "1.32", "0.014"),
        ],
    )
    def test_published_rates_are_recorded_exactly(self, model, rate_in, rate_out, rate_cached):
        """The vendor's peak figures, transcribed. Asserted literally rather
        than as a relationship between models: pro is 3x flash on input and
        output but NOT on the cache-hit rate (0.044 against 0.014), so a test
        that checked the ratio would enforce a tidiness the price list does not
        have and would have to be loosened the first time it was right."""
        tier = next(
            m for m in providers.PROVIDERS["deepseek"].models if m.name == model
        ).rates.tiers[0]
        assert tier.rate_in == Decimal(rate_in)
        assert tier.rate_out == Decimal(rate_out)
        assert tier.rate_cached_in == Decimal(rate_cached)

    def test_a_cache_hit_is_far_cheaper_than_the_global_assumption(self):
        """3.2% of the input rate, not the 10% the old global multiplier
        applied to every provider. This is the case that justifies a per-tier
        cached rate rather than one constant."""
        tier = providers.PROVIDERS["deepseek"].models[0].rates.tiers[0]
        assert tier.rate_cached_in is not None
        assert tier.rate_cached_in / tier.rate_in < Decimal("0.05")


class TestPeakWindows:
    @pytest.mark.parametrize(
        ("when", "off_peak"),
        [
            # Peak is 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday.
            (_at(MON, 0, 59), True),
            (_at(MON, 1), False),
            (_at(MON, 3, 59), False),
            # The boundary the half-open rule exists for: 04:00:00 is off-peak.
            (_at(MON, 4), True),
            (_at(MON, 5), True),
            (_at(MON, 6), False),
            (_at(MON, 9, 59), False),
            (_at(MON, 10), True),
            (_at(MON, 23), True),
            # Weekends are off-peak at every hour, including peak-hour ones.
            (_at(SAT, 3), True),
            (_at(SAT, 7), True),
        ],
    )
    def test_window_membership(self, when, off_peak):
        rates = providers.PROVIDERS["deepseek"].models[0].rates
        assert pricing.is_off_peak(rates, when) is off_peak

    def test_monday_midnight_is_off_peak_on_both_sides_of_the_week_boundary(self):
        """Sunday 23:59 and Monday 00:00 are both outside every window. The
        assertion is about the day NUMBERING: isodow makes Sunday 7 and Monday
        1, and a rendering that used Python's 0-based weekday() would shift
        every window by a day and misprice one day a week in silence."""
        rates = providers.PROVIDERS["deepseek"].models[0].rates
        sunday_late = _at(datetime.date(2026, 9, 6), 23, 59)
        assert pricing.is_off_peak(rates, sunday_late) is True
        assert pricing.is_off_peak(rates, _at(MON, 0)) is True
        # ...and the first peak hour of the week is Monday 01:00, not Sunday's.
        assert pricing.is_off_peak(rates, _at(MON, 1)) is False
        assert pricing.is_off_peak(rates, _at(datetime.date(2026, 9, 6), 1)) is True

    def test_an_unknown_timestamp_bills_at_peak(self):
        """The same direction as an unpublished cached rate: overstate rather
        than invent a discount. A wrong low number is worse than a wrong high
        one on the surface built to make spend visible."""
        rates = providers.PROVIDERS["deepseek"].models[0].rates
        assert pricing.is_off_peak(rates, None) is False
        assert pricing.estimate_cost_usd(FLASH, 1_000_000, 0, at=None) == Decimal("0.44")

    def test_off_peak_is_exactly_half(self):
        peak = pricing.estimate_cost_usd(FLASH, 1_000_000, 1_000_000, at=_at(MON, 3))
        off = pricing.estimate_cost_usd(FLASH, 1_000_000, 1_000_000, at=_at(MON, 12))
        assert peak is not None and off is not None
        assert off == peak / 2

    def test_the_discount_applies_to_cached_input_too(self):
        """off_peak_multiplier is documented as multiplying all three rates.
        A cached-heavy call must move with the window like any other."""
        kw = dict(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=1_000_000)
        peak = pricing.estimate_cost_usd(FLASH, at=_at(MON, 3), **kw)
        off = pricing.estimate_cost_usd(FLASH, at=_at(MON, 12), **kw)
        assert peak is not None and off is not None
        assert off == peak / 2

    def test_a_provider_without_windows_is_never_off_peak(self):
        rates = providers.PROVIDERS["openai"].models[0].rates
        assert rates.peak_windows == ()
        assert pricing.is_off_peak(rates, _at(MON, 12)) is False
        assert pricing.off_peak_sql(rates, "created_at") == "FALSE"


_PARITY_TIMES = [
    _at(MON, 3),  # peak
    _at(MON, 4),  # the half-open boundary
    _at(MON, 12),  # off-peak weekday
    _at(SAT, 3),  # off-peak weekend, in a peak-hour slot
    _at(datetime.date(2026, 9, 6), 23, 59),  # Sunday, the week-boundary case
]


@pytest.mark.parametrize("when", _PARITY_TIMES)
@pytest.mark.parametrize(
    ("prompt", "completion", "cached"), [(1_000_000, 250_000, 400_000), (3, 7, 1)]
)
def test_sql_and_python_agree_on_wall_clock_pricing(when, prompt, completion, cached):
    """The parity test that matters most here, because the two renderings use
    different day numbering primitives. Python reads isoweekday(); Postgres
    reads EXTRACT(isodow). If they ever disagree, one day a week is priced at
    double or half and nothing else would show it."""
    rates = pricing.rates_for(FLASH)
    assert rates is not None
    tier = rates.tiers[0]
    expr = pricing.cost_sql(
        model_rate_in="%(rate_in)s::numeric",
        model_rate_out="%(rate_out)s::numeric",
        model_rate_cached_in="%(rate_cached)s::numeric",
        batch_rate="1",
        prompt="%(prompt)s::bigint",
        completion="%(completion)s::bigint",
        cached="%(cached)s::bigint",
        batched="FALSE",
        off_peak=pricing.off_peak_sql(rates, "%(at)s::timestamptz"),
        off_peak_multiplier="%(off_peak_multiplier)s::numeric",
    )
    row = db.query_one(
        f"SELECT {expr} AS cost",
        {
            "rate_in": str(tier.rate_in),
            "rate_out": str(tier.rate_out),
            "rate_cached": str(pricing.cached_rate(tier)),
            "off_peak_multiplier": str(rates.off_peak_multiplier),
            "prompt": prompt,
            "completion": completion,
            "cached": cached,
            "at": when.isoformat(),
        },
    )
    assert row is not None
    py = pricing.estimate_cost_usd(FLASH, prompt, completion, cached_tokens=cached, at=when)
    assert py is not None
    assert Decimal(row["cost"]) == py


class _Reply(BaseModel):
    ok: bool
    note: str = ""


class _FakeCompletions:
    """Records what was sent and returns what the caller was told to expect."""

    def __init__(self, content: str, finish_reason: str = "stop"):
        self.content, self.finish_reason, self.sent = content, finish_reason, None

    async def create(self, **kwargs):
        self.sent = kwargs

        class _M:
            content = self.content

        class _C:
            message = _M()
            finish_reason = self.finish_reason

        class _U:
            prompt_tokens = completion_tokens = total_tokens = 10
            prompt_tokens_details = completion_tokens_details = None

        class _R:
            choices = (_C(),)
            usage = _U()

        return _R()


class _FakeClient:
    def __init__(self, completions):
        self.chat = type("chat", (), {"completions": completions})()


class TestJsonObjectPath:
    """The branch DeepSeek needs and the strict-schema path cannot give it.

    Dispatched on the declared MODE, not the provider name, so these assertions
    are about a capability rather than about DeepSeek specifically.
    """

    def _cfg(self, **params):
        return ai.AIConfig(
            provider="deepseek", api_key="k", key_source="owner", model=FLASH, params=params
        )

    async def _run(self, fake, instructions="Extract the fields.", **params):
        declared = providers.model(FLASH)
        assert declared is not None
        return await ai._parse_json_object(
            _FakeClient(fake),
            self._cfg(**params),
            instructions,
            "some page text",
            _Reply,
            declared,
            30.0,
        )

    @pytest.mark.asyncio
    async def test_the_literal_word_is_added_when_missing(self):
        """DeepSeek refuses the request outright without it, so a caller
        forgetting is a 400 rather than a worse answer."""
        fake = _FakeCompletions('{"ok": true}')
        await self._run(fake, instructions="Extract the compensation.")
        assert fake.sent is not None
        assert "json" in fake.sent["messages"][0]["content"].lower()
        assert fake.sent["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_an_instruction_that_already_says_json_is_left_alone(self):
        fake = _FakeCompletions('{"ok": true}')
        original = "Reply in json with the fields."
        await self._run(fake, instructions=original)
        assert fake.sent is not None
        assert fake.sent["messages"][0]["content"] == original

    @pytest.mark.asyncio
    async def test_the_declared_reasoning_default_is_sent_when_unset(self):
        """Unset is not neutral on DeepSeek: it costs 79 extra input tokens a
        call against 'low'. Omitting the parameter pays that by omission."""
        fake = _FakeCompletions('{"ok": true}')
        await self._run(fake)
        assert fake.sent is not None
        assert fake.sent["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_a_callers_choice_beats_the_default(self):
        fake = _FakeCompletions('{"ok": true}')
        await self._run(fake, reasoning_effort="high")
        assert fake.sent is not None
        assert fake.sent["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_the_response_is_validated_against_the_model(self):
        """The provider guarantees the bytes parse as JSON, nothing more. A
        json_object response that does not match the model asked for has to be
        caught here, because nothing upstream enforced it."""
        fake = _FakeCompletions('{"wrong_field": 1}')
        with pytest.raises(ValidationError):
            await self._run(fake)

    @pytest.mark.asyncio
    async def test_a_truncated_response_is_refused_not_parsed(self):
        """finish_reason='length' with a 200 and half a JSON object. Parsing it
        would raise pointing at a column in a string rather than at the token
        budget that caused it."""
        fake = _FakeCompletions('{"ok": tr', finish_reason="length")
        with pytest.raises(ValueError, match="output limit"):
            await self._run(fake)

    @pytest.mark.asyncio
    async def test_a_good_response_parses(self):
        fake = _FakeCompletions('{"ok": true, "note": "hi"}')
        parsed, usage = await self._run(fake)
        assert parsed == _Reply(ok=True, note="hi")
        assert usage["prompt_tokens"] == 10
