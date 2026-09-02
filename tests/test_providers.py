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
