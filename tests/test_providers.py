"""The provider descriptors, and the invariants that make them reviewable.

Kanishk reviews these roughly monthly. That only works if the registry cannot
accumulate numbers nobody can trace, so the provenance rules are asserted here
rather than left to a comment convention: a comment cannot fail CI.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api import ai
from core import pricing, providers
from core.providers.spec import Rates, Source, Tier


def _all_models():
    for name, provider in providers.PROVIDERS.items():
        for m in provider.models:
            yield name, m


def test_every_rate_carries_a_source():
    """An undated number cannot be re-checked, only re-guessed."""
    for provider_name, m in _all_models():
        assert m.rates.source is not None, f"{provider_name}/{m.name} has no rate source"
        assert isinstance(m.rates.source, Source)


def test_a_batch_rate_requires_its_own_provenance():
    """Rates and batch discounts come from different places - one vendor
    publishes its per-token rates plainly and says nothing about its batch
    lane - so claiming a discount without citing where it came from is not
    allowed."""
    for provider_name, m in _all_models():
        if m.rates.batch_rate is not None:
            assert m.rates.batch_source is not None, (
                f"{provider_name}/{m.name} claims a batch discount with no source"
            )


def test_every_reasoning_declaration_carries_a_source():
    for provider_name, m in _all_models():
        assert m.reasoning.source is not None, f"{provider_name}/{m.name} reasoning has no source"


def test_declared_efforts_do_not_contradict_themselves():
    for provider_name, m in _all_models():
        overlap = set(m.reasoning.accepts) & set(m.reasoning.rejects)
        assert not overlap, f"{provider_name}/{m.name} both accepts and rejects {overlap}"


def test_model_names_are_globally_unique():
    """The only key a usage row carries is the model string - neither
    ai_queries nor api_usage records a provider - so two providers publishing
    the same name would silently cross-price. The registry refuses to build
    instead, which is also what stops a second region being added for a model
    without first recording which endpoint served the call."""
    seen = [m.name for _, m in _all_models()]
    assert len(seen) == len(set(seen))


def test_tiers_are_ordered_and_terminated():
    """Tier lookup walks in order and returns the first match, so an unordered
    list would price a large prompt at a small prompt's rate. The last tier
    must be open-ended or a long enough prompt falls off the end."""
    for provider_name, m in _all_models():
        bounds = [t.up_to_prompt_tokens for t in m.rates.tiers]
        assert bounds[-1] is None, f"{provider_name}/{m.name} has no open-ended final tier"
        finite = [b for b in bounds if b is not None]
        assert finite == sorted(finite), f"{provider_name}/{m.name} tiers are out of order"
        assert all(b is not None for b in bounds[:-1]), (
            f"{provider_name}/{m.name} has an open-ended tier before the last"
        )


# --- the wrong-number guard ---------------------------------------------


def test_batched_equals_sync_for_a_model_with_no_batch_lane(monkeypatch):
    """The failure this exists to prevent: a global 0.5 multiplier applied to a
    model whose vendor never discounted it reports HALF the true cost, silently,
    on the surface built to make cost visible. That is a WRONG number, which is
    worse than the NULL an unpriced model books - a gap at least admits itself.

    Asserted against a synthetic model rather than a real one so it keeps
    testing the rule after the catalogue changes. It fails against the code
    this replaced, which multiplied by 0.5 unconditionally.
    """
    no_lane = Rates(
        tiers=(Tier(None, Decimal("10"), Decimal("20"), Decimal("1")),),
        batch_rate=None,
        source=Source(url="", read_on=None, vendor=False),
    )
    monkeypatch.setattr(pricing, "rates_for", lambda model: no_lane)
    batched = pricing.estimate_cost_usd("anything", 1_000_000, 1_000_000, batched=True)
    sync = pricing.estimate_cost_usd("anything", 1_000_000, 1_000_000)
    assert batched == sync
    assert batched == Decimal(30)


def test_an_unpublished_cached_rate_bills_at_the_full_input_rate(monkeypatch):
    """None means the vendor does not publish one, never that caching is free."""
    unpublished = Rates(
        tiers=(Tier(None, Decimal("10"), Decimal("20"), None),),
        batch_rate=None,
        source=Source(url="", read_on=None, vendor=False),
    )
    monkeypatch.setattr(pricing, "rates_for", lambda model: unpublished)
    cached = pricing.estimate_cost_usd("anything", 1_000_000, 0, cached_tokens=1_000_000)
    uncached = pricing.estimate_cost_usd("anything", 1_000_000, 0)
    assert cached == uncached == Decimal(10)


# --- tier selection ------------------------------------------------------


def test_tier_is_selected_by_prompt_length(monkeypatch):
    tiered = Rates(
        tiers=(
            Tier(1_000, Decimal("1"), Decimal("2"), Decimal("0")),
            Tier(None, Decimal("10"), Decimal("20"), Decimal("0")),
        ),
        batch_rate=None,
        source=Source(url="", read_on=None, vendor=False),
    )
    monkeypatch.setattr(pricing, "rates_for", lambda model: tiered)
    # At the boundary the cheaper tier still applies; one token past it, the
    # whole request - output included - moves up.
    assert pricing.estimate_cost_usd("m", 1_000, 1_000) == (
        Decimal(1_000) * 1 + Decimal(1_000) * 2
    ) / Decimal(1_000_000)
    assert pricing.estimate_cost_usd("m", 1_001, 1_000) == (
        Decimal(1_001) * 10 + Decimal(1_000) * 20
    ) / Decimal(1_000_000)


def test_is_tiered_reports_the_shape():
    assert pricing.is_tiered("gpt-5-nano") is False
    assert pricing.is_tiered("no-such-model") is False


# --- reasoning validation ------------------------------------------------


def test_a_model_is_validated_against_its_own_declared_set():
    """The split a union hid: the 5.6 generation rejects 'minimal' with a 400
    and the earlier generation accepts it. One tuple covering both accepts a
    value half the catalogue refuses."""
    assert ai.validate_params("openai", {"reasoning_effort": "minimal"}, "gpt-5") is None
    assert ai.validate_params("openai", {"reasoning_effort": "minimal"}, "gpt-5.6-luna") is not None
    assert ai.validate_params("openai", {"reasoning_effort": "none"}, "gpt-5.6-luna") is None
    assert ai.validate_params("openai", {"reasoning_effort": "none"}, "gpt-5") is not None


def test_an_unknown_model_is_not_validated_at_all():
    """A model that shipped after this table was last read must not be blocked
    by the table's ignorance. A wrong rejection reads as a mystery outage; a
    wrong acceptance comes back as the vendor's own error naming what it takes.
    """
    assert (
        ai.validate_params("openai", {"reasoning_effort": "whatever"}, "gpt-9-unreleased") is None
    )


def test_without_a_model_the_provider_union_still_applies():
    """The settings route does not always know the model. The union is the
    permissive fallback - but derived from the per-model declarations now,
    not maintained beside them."""
    assert ai.validate_params("openai", {"reasoning_effort": "medium"}) is None
    assert ai.validate_params("openai", {"reasoning_effort": "bogus"}) is not None


def test_catalog_and_defaults_are_projected_from_the_registry():
    for name, provider in providers.PROVIDERS.items():
        assert [m["model"] for m in ai.MODEL_CATALOG[name]] == [
            m.name for m in provider.models if m.selectable
        ]
        assert ai.DEFAULT_MODELS[name] == provider.models[0].name
    assert "openai_compatible" in ai.MODEL_CATALOG
    assert ai.DEFAULT_MODELS["openai_compatible"] is None


@pytest.mark.parametrize("model", [m.name for _, m in _all_models()])
def test_every_catalogued_model_can_be_priced(model):
    assert pricing.estimate_cost_usd(model, 1_000, 1_000) is not None


def test_an_internal_model_is_priced_but_never_offered():
    """Being unofferable and being unpriced are different things. The
    embedding model has no chat interface to put in a picker, and it still
    costs money - conflating the two would either expose it as a filter model
    or make its spend invisible."""
    embedding = providers.model("text-embedding-3-small")
    assert embedding is not None and embedding.selectable is False
    assert pricing.rates_for("text-embedding-3-small") is not None
    offered = {m["model"] for models in ai.MODEL_CATALOG.values() for m in models}
    assert "text-embedding-3-small" not in offered


# --- xAI, the provider that proves wire format is not contract -----------


def test_xai_tiering_rebills_the_whole_request_including_output():
    """xAI's high tier applies to output as well as input once the PROMPT
    crosses the threshold, so a long prompt with a short answer still pays the
    higher output rate. A tier that only moved the input rate would understate
    every one of these."""
    small = pricing.estimate_cost_usd("grok-4.6", 200_000, 1_000)
    large = pricing.estimate_cost_usd("grok-4.6", 200_001, 1_000)
    assert small is not None and large is not None
    # One extra prompt token roughly doubles the bill, output included.
    assert large > small * Decimal("1.9")


def test_two_models_of_one_provider_disagree_about_reasoning():
    """The sharpest case for declaring per model rather than per provider:
    grok-4.3 accepts "none" and grok-4.6 rejects it, same provider, same
    generation family. A provider-level union would accept it for both and
    400 on one."""
    assert ai.validate_params("xai", {"reasoning_effort": "none"}, "grok-4.3") is None
    assert ai.validate_params("xai", {"reasoning_effort": "none"}, "grok-4.6") is not None
    # And a value OpenAI's 5.6 family takes that no Grok does.
    assert ai.validate_params("xai", {"reasoning_effort": "max"}, "grok-4.3") is not None


def test_temperature_follows_the_declared_capability_not_a_provider_name():
    """xAI accepts temperature and OpenAI's Responses API does not. Confirmed
    live for xAI; the gate reads the descriptor rather than naming providers."""
    assert providers.PROVIDERS["xai"].supports_temperature is True
    assert providers.PROVIDERS["openai"].supports_temperature is False
    assert ai.validate_params("xai", {"temperature": 0.5}) is None
    assert ai.validate_params("openai", {"temperature": 0.5}) is not None


def test_xai_has_a_batch_lane_but_no_declared_discount():
    """The lane exists - GET /v1/batches returns 200 - but no vendor page
    states a discount, so none is claimed. A batched call therefore bills at
    the synchronous rate: overstating if the reported 20% is real, which is the
    safe direction and the opposite of what a global 0.5 would have done."""
    assert providers.PROVIDERS["xai"].batch_endpoint == "/v1/batches"
    for model in ("grok-4.3", "grok-4.5", "grok-4.6"):
        rates = pricing.rates_for(model)
        assert rates is not None and rates.batch_rate is None
        batched = pricing.estimate_cost_usd(model, 10_000, 10_000, batched=True)
        assert batched == pricing.estimate_cost_usd(model, 10_000, 10_000)


def test_xai_cached_rate_is_not_the_openai_tenth():
    """grok-4.6 bills cached input at 25% of input, not 10%. The global
    multiplier this package replaced would have understated it 2.5x."""
    tier = pricing.rates_for("grok-4.6").tiers[0]
    assert tier.rate_cached_in / tier.rate_in == Decimal("0.25")
    openai_tier = pricing.rates_for("gpt-5-nano").tiers[0]
    assert openai_tier.rate_cached_in / openai_tier.rate_in == Decimal("0.1")
