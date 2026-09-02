def test_every_modelled_provider_is_offerable_on_the_owner_key(client, user_headers, monkeypatch):
    """The owner-key branch listed ("openai", "anthropic") from before xAI and
    DeepSeek existed, so two fully modelled and fully priced providers were
    invisible on this page while being selectable everywhere else."""
    from api.routers import users as users_router

    seen = []

    def fake_allowed(groups):
        from core import providers

        for p in providers.PROVIDERS.values():
            for m in p.models:
                if m.selectable:
                    seen.append(m.name)
        return sorted(seen)

    monkeypatch.setattr(users_router.budget, "owner_allowed_models", fake_allowed)
    body = client.get("/v1/models", headers=user_headers).json()
    offered = {p["provider"] for p in body["providers"]}
    assert {"openai", "anthropic", "xai", "deepseek"} <= offered


def test_a_model_carries_what_its_datasheet_declares(client, user_headers):
    """The page showed a name and a note while the provider modules carried
    rates, context, batch eligibility and accepted reasoning values - so
    choosing a model meant guessing at everything that distinguishes one."""
    from api.routers.users import _catalog

    nano = next(m for m in _catalog("openai") if m["model"] == "gpt-5-nano")
    assert nano["rate_in_per_mtok"] == 0.05
    assert nano["batch_discount"] == 0.5
    assert nano["structured_output"] == "json_schema"
    assert "minimal" in nano["reasoning_accepts"]

    # DeepSeek cannot enforce a schema, only valid JSON. That is the single
    # most consequential difference between these models for this codebase and
    # it was nowhere on the page.
    flash = next(m for m in _catalog("deepseek") if m["model"] == "deepseek-v4-flash")
    assert flash["structured_output"] == "json_object"
    assert flash["batch_discount"] is None, "no batch endpoint is not a 1.0 discount"


def test_params_come_from_the_datasheets_not_a_hardcoded_map(client, user_headers):
    """_PROVIDER_PARAMS listed three providers and never gained the two that
    came later, so offering them would have raised KeyError on a page that only
    ever showed two. The reasoning parameter is per MODEL, so the provider
    offers the union and validate_params still checks the specific one."""
    from api.routers.users import _provider_params

    assert _provider_params("openai") == ["reasoning_effort", "max_output_tokens"]
    assert _provider_params("anthropic") == ["effort", "max_output_tokens"]
    # Only these two take a temperature, and both are new since the map.
    assert "temperature" in _provider_params("xai")
    assert "temperature" in _provider_params("deepseek")
    assert "temperature" not in _provider_params("openai")
    # A user-supplied endpoint has no datasheet, so it gets the portable
    # minimum rather than a KeyError.
    assert _provider_params("openai_compatible") == ["temperature", "max_output_tokens"]


def test_an_unlooked_up_rate_stays_null_rather_than_zero(client, user_headers):
    """None means nobody has looked it up. Rendering it as 0 would make an
    unknown cost look free, which is the same failure as an unpriced model
    booking as zero spend."""
    from api.routers.users import _catalog

    for provider in ("openai", "anthropic", "xai", "deepseek"):
        for m in _catalog(provider):
            assert m["rate_in_per_mtok"] is None or m["rate_in_per_mtok"] > 0
