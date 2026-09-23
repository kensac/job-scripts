"""Regression cases from vendor documentation reviewed September 23, 2026."""

from decimal import Decimal

import pytest

from api import ai
from core import pricing, providers


def test_catalog_distinguishes_vendor_batch_from_application_transport():
    from api.routers.users import _catalog

    anthropic = {m["model"]: m for m in _catalog("anthropic")}
    opus = anthropic["claude-opus-5"]
    assert opus["vendor_batch_supported"] is True
    assert opus["batch_available"] is False
    assert "not implemented" in opus["batch_unavailable_reason"]
    assert opus["cache_write_5m_per_mtok"] == 6.25
    assert opus["cache_write_1h_per_mtok"] == 10
    assert opus["rate_cached_in_per_mtok"] == 0.5


@pytest.mark.asyncio
async def test_unsupported_batch_never_uses_openai_credentials(monkeypatch):
    from core import batch

    def wrong_provider_client():
        pytest.fail("a non-OpenAI model reached the OpenAI batch client")

    monkeypatch.setattr(batch, "_client", wrong_provider_client)
    with pytest.raises(ValueError, match="BATCH_UNSUPPORTED"):
        await batch.submit_batches([batch.BatchSpec("one")], "grok-4.3", "low", 256)


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"])
def test_gpt56_long_context_prices_the_full_request(model):
    before = pricing.estimate_cost_usd(model, 272_000, 1_000, cache_write_tokens=0)
    after = pricing.estimate_cost_usd(model, 272_001, 1_000, cache_write_tokens=0)
    assert after > before * Decimal("1.9")


@pytest.mark.parametrize(
    ("name", "input_rate", "output_rate", "cached_rate"),
    [
        ("claude-fable-5-1", "10", "50", "0.25"),
        ("claude-opus-5-5", "4", "20", "0.20"),
        ("grok-4.7", "2", "6", "0.50"),
        ("grok-build-0.1", "1", "2", "0.20"),
        ("deepseek-flash", "0.30", "1.20", "0.006"),
    ],
)
def test_current_models_have_vendor_verified_prices(name, input_rate, output_rate, cached_rate):
    model = providers.model(name)
    assert model is not None
    assert model.rates.source.vendor
    assert model.rates.source.read_on is not None
    tier = model.rates.tiers[0]
    assert (tier.rate_in, tier.rate_out, tier.rate_cached_in) == tuple(
        map(Decimal, (input_rate, output_rate, cached_rate))
    )


def test_haiku_does_not_accept_an_effort_control():
    assert ai.validate_params("anthropic", {"effort": "high"}, "claude-haiku-4-5")


def test_grok_discount_is_model_specific():
    assert providers.model("grok-4.3").rates.batch_rate == Decimal("0.8")
    assert providers.model("grok-4.7").rates.batch_rate is None


def test_grok_long_context_starts_at_200k():
    before = pricing.estimate_cost_usd("grok-4.6", 199_999, 1_000)
    at = pricing.estimate_cost_usd("grok-4.6", 200_000, 1_000)
    assert at > before * Decimal("1.9")
