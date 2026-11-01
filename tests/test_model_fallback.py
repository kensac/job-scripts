"""A provider out of credit moves work down the purpose's fallback chain.

On 2026-10-31 an OpenAI billing hard limit refused every batch submission
from 19:05 to 23:17 UTC: 374 filter chunks failed, and batches accepted just
before it returned 33,365 refused requests. Nothing moved the work.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from api import db, model_fallback
from api.model_calls import Payer
from core import batch
from core.batch.collection import _line_error
from core.fallback import (
    ProviderExhausted,
    custom_filter_shape,
    exhaustion_reason,
    next_choice,
)
from core.shapes import COMP_TASK, VERIFY_TASK

_REQUEST = httpx2.Request("POST", "https://api.example.com/v1/batches")


def _openai_error(status: int, body: dict) -> Exception:
    """The exception the OpenAI SDK itself raises for this response."""
    response = httpx2.Response(status, request=_REQUEST, content=json.dumps(body).encode())
    return AsyncOpenAI(api_key="k")._make_status_error_from_response(response)


def _anthropic_error(status: int, body: dict) -> Exception:
    response = httpx2.Response(status, request=_REQUEST, content=json.dumps(body).encode())
    return AsyncAnthropic(api_key="k")._make_status_error_from_response(response)


def _deepseek_error(status: int, body: dict) -> Exception:
    """DeepSeek is called through the OpenAI SDK at its own base url."""
    return _openai_error(status, body)


EXHAUSTED = [
    # The worker log line of 2026-10-31, exactly as core.batch logged it.
    ("openai", "Error code: 400 - Billing hard limit has been reached"),
    (
        "openai",
        _openai_error(
            400,
            {
                "error": {
                    "message": "Billing hard limit has been reached",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "billing_hard_limit_reached",
                }
            },
        ),
    ),
    # insufficient_quota is a 429: it must not be read as a rate limit.
    (
        "openai",
        _openai_error(
            429,
            {
                "error": {
                    "message": "You exceeded your current quota, please check your plan and "
                    "billing details.",
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
        ),
    ),
    # A refused request inside an accepted batch, as collection stores it.
    (
        "openai",
        _line_error(
            {
                "custom_id": "u1",
                "error": None,
                "response": {
                    "status_code": 400,
                    "body": {"error": {"message": "Billing hard limit has been reached"}},
                },
            }
        )
        or "",
    ),
    (
        "anthropic",
        _anthropic_error(
            400,
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API. "
                    "Please go to Plans & Billing to upgrade or purchase credits.",
                },
            },
        ),
    ),
    (
        "anthropic",
        _anthropic_error(
            402,
            {"type": "error", "error": {"type": "billing_error", "message": "Billing issue"}},
        ),
    ),
    # An errored Anthropic batch result, as anthropic_batches stores it.
    ("anthropic", "billing_error: Billing issue"),
    (
        "deepseek",
        _deepseek_error(
            402,
            {
                "error": {
                    "message": "Insufficient Balance",
                    "type": "unknown_error",
                    "param": None,
                    "code": "invalid_request_error",
                }
            },
        ),
    ),
]

TRANSIENT = [
    (
        "openai",
        _openai_error(
            429,
            {
                "error": {
                    "message": "Rate limit reached for gpt-6-luna on tokens per min (TPM): "
                    "Limit 200000, Used 199000, Requested 2500.",
                    "type": "tokens",
                    "code": "rate_limit_exceeded",
                }
            },
        ),
    ),
    (
        "openai",
        _openai_error(
            400,
            {
                "error": {
                    "message": "Enqueued token limit reached for gpt-6-luna in organization "
                    "org-x. Limit: 40,000,000 enqueued tokens.",
                    "type": "invalid_request_error",
                    "code": "token_limit_exceeded",
                }
            },
        ),
    ),
    ("openai", _openai_error(500, {"error": {"message": "The server had an error"}})),
    ("openai", "Request timed out."),
    (
        "anthropic",
        _anthropic_error(
            529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        ),
    ),
    (
        "anthropic",
        _anthropic_error(
            429,
            {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited"}},
        ),
    ),
    # No shape recorded for xAI, so nothing it says is exhaustion.
    ("xai", "Error code: 429 - Your team has used all available credits"),
    # One vendor's wording says nothing about another vendor's account.
    ("anthropic", "Error code: 400 - Billing hard limit has been reached"),
    ("openai", "invalid_request_error: Your credit balance is too low"),
]


@pytest.mark.parametrize(("provider", "error"), EXHAUSTED)
def test_each_vendors_exhaustion_shape_is_recognised(provider, error):
    assert exhaustion_reason(provider, error)


@pytest.mark.parametrize(("provider", "error"), TRANSIENT)
def test_rate_limits_outages_and_other_vendors_words_are_not_exhaustion(provider, error):
    assert exhaustion_reason(provider, error) is None


def test_the_chain_skips_exhausted_providers_and_ineligible_models_in_order():
    # deepseek cannot enforce a schema, grok has no batch transport here.
    chain = ["deepseek-v4-flash", "grok-4.3", "gpt-5-mini", "claude-haiku-5-5", "claude-sonnet-5"]
    first = next_choice(COMP_TASK, chain, blocked=set(), tried={"gpt-6-luna"})
    assert first is not None and first.model == "gpt-5-mini"
    skipped = next_choice(COMP_TASK, chain, blocked={"openai"}, tried={"gpt-6-luna"})
    assert skipped is not None and skipped.model == "claude-haiku-5-5"
    again = next_choice(COMP_TASK, chain, {"openai"}, {"gpt-6-luna", "claude-haiku-5-5"})
    assert again is not None and again.model == "claude-sonnet-5"
    assert next_choice(COMP_TASK, chain, {"openai", "anthropic"}, set()) is None
    assert next_choice(COMP_TASK, [], set(), set()) is None


LUNA_HAIKU = [
    {"kind": "custom", "models": ["gpt-6-luna", "claude-haiku-5-5"]},
    {"kind": "verify", "models": ["gpt-6-luna", "claude-haiku-5-5"]},
]


@pytest.mark.parametrize("purpose", ["managed_board", "filter", "verify"])
def test_a_verdict_purpose_falls_back_only_inside_the_primarys_group(
    purpose, set_config, monkeypatch
):
    """Any other model's verdicts would be paid for and read by nobody."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    shape = (
        VERIFY_TASK if purpose == "verify" else custom_filter_shape("gpt-6-luna", "low", purpose)
    )
    set_config("model_fallback_chains", {purpose: ["claude-sonnet-5", "claude-haiku-5-5"]})
    blocked = {"openai"}
    assert model_fallback.next_for(shape, "gpt-6-luna", {"gpt-6-luna"}, blocked) is None
    set_config("verdict_model_equivalence", LUNA_HAIKU)
    choice = model_fallback.next_for(shape, "gpt-6-luna", {"gpt-6-luna"}, blocked)
    assert choice is not None
    assert (choice.model, choice.fallback_from) == ("claude-haiku-5-5", "gpt-6-luna")


def test_a_purpose_that_writes_no_verdicts_may_use_any_entry(set_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    set_config("model_fallback_chains", {"comp": ["claude-sonnet-5", "claude-haiku-5-5"]})
    choice = model_fallback.next_for(COMP_TASK, "gpt-6-luna", {"gpt-6-luna"}, {"openai"})
    assert choice is not None and choice.model == "claude-sonnet-5"


def test_an_effort_the_fallback_does_not_take_makes_it_ineligible():
    chain = ["claude-haiku-5-5"]
    assert next_choice(custom_filter_shape("gpt-6-luna", "low"), chain, set(), set())
    assert next_choice(custom_filter_shape("gpt-6-luna", "minimal"), chain, set(), set()) is None


@pytest.mark.asyncio
async def test_a_refused_wave_stops_the_submission_and_keeps_the_accepted_ones(monkeypatch):
    monkeypatch.setattr(batch, "_client", object)
    monkeypatch.setattr(batch, "BATCH_MAX_REQUESTS", 1)
    calls = []

    async def submit(client, chunk, *args):
        calls.append(chunk[0].custom_id)
        if len(calls) == 1:
            return "b1"
        raise RuntimeError("Error code: 400 - Billing hard limit has been reached")

    monkeypatch.setattr(batch, "_submit_chunk", submit)
    specs = [batch.BatchSpec(cid, "text") for cid in ("a", "b", "c")]
    with pytest.raises(ProviderExhausted) as raised:
        await batch.submit_batches(specs, "gpt-6-luna", "low", 256)
    assert calls == ["a", "b"], "nothing is sent after the refusal"
    assert raised.value.accepted == ["b1"]
    assert [s.custom_id for s in raised.value.unsubmitted] == ["b", "c"]
    assert raised.value.provider == "openai"


def _alert(provider: str) -> dict | None:
    return db.query_one(
        "SELECT message, detail, resolved_at FROM health_alerts "
        "WHERE kind = 'provider_exhausted' AND subject = %s ORDER BY id DESC LIMIT 1",
        (provider,),
    )


@pytest.mark.asyncio
async def test_refused_requests_resubmit_on_the_next_model_in_the_same_sweep(
    set_config, monkeypatch
):
    from tasks.runtime import batching

    set_config("model_fallback_chains", {"comp": ["grok-4.3", "claude-haiku-5-5"]})
    set_config("fleet_weekly_cycles", 0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(batching, "has_batch_work", lambda _task_id: False)
    monkeypatch.setattr(batching, "snapshot_specs", lambda _task_id, specs: specs)
    parked = {}

    def park(task_id, ids):
        parked[task_id] = ids
        return True

    monkeypatch.setattr(batching, "park_awaiting_batch", park)
    sent = []

    async def submit_batches(specs, model, effort, max_tokens, on_event=None):
        sent.append((model, effort, [s.custom_id for s in specs]))
        if model == "gpt-6-luna":
            on_event("batch_fb_1", "validating", {"requests": 1})
            raise ProviderExhausted(
                "openai", "Billing hard limit has been reached", ["batch_fb_1"], specs[1:]
            )
        on_event("batch_fb_2", "validating", {"requests": len(specs)})
        return ["batch_fb_2"]

    monkeypatch.setattr("core.batch.submit_batches", submit_batches)
    specs = [batch.BatchSpec(cid, "text") for cid in ("a", "b", "c")]
    hook = batching.batch_event_hook(987654, "comp", "gpt-6-luna")
    with pytest.raises(batching.AwaitingBatch):
        await batching.submit_or_collect(
            987654,
            specs,
            "gpt-6-luna",
            "low",
            1500,
            hook,
            fallback=COMP_TASK,
            purpose="comp",
        )

    assert sent == [("gpt-6-luna", "low", ["a", "b", "c"]), ("claude-haiku-5-5", "low", ["b", "c"])]
    assert parked[987654] == ["batch_fb_1", "batch_fb_2"]
    rows = {
        r["provider_batch_id"]: r
        for r in db.query(
            "SELECT provider_batch_id, model, fallback_from FROM ai_batches WHERE task_id = 987654"
        )
    }
    assert (
        rows["batch_fb_1"]["model"] == "gpt-6-luna" and rows["batch_fb_1"]["fallback_from"] is None
    )
    assert rows["batch_fb_2"]["model"] == "claude-haiku-5-5"
    assert rows["batch_fb_2"]["fallback_from"] == "gpt-6-luna"
    alert = _alert("openai")
    assert alert and alert["resolved_at"] is None
    assert alert["detail"]["reason"] == "Billing hard limit has been reached"
    assert "openai" in model_fallback.blocked_providers()


@pytest.mark.asyncio
async def test_a_managed_board_resubmits_on_a_grouped_model_and_bills_the_board(
    set_config, monkeypatch
):
    """Public boards stalled through the 2026-10-31 outage. A board reads its
    verdicts through answering_models, so a grouped model's answers show."""
    from tasks.runtime import batching

    set_config("verdict_model_equivalence", LUNA_HAIKU)
    set_config("model_fallback_chains", {"managed_board": ["claude-sonnet-5", "claude-haiku-5-5"]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(batching, "has_batch_work", lambda _task_id: False)
    monkeypatch.setattr(batching, "snapshot_specs", lambda _task_id, specs: specs)
    monkeypatch.setattr(batching, "park_awaiting_batch", lambda _task_id, _ids: True)
    sent = []

    async def submit_batches(specs, model, effort, max_tokens, on_event=None):
        sent.append(model)
        if model == "gpt-6-luna":
            raise ProviderExhausted("openai", "Billing hard limit has been reached", [], specs)
        on_event("batch_mb_1", "validating", {"requests": len(specs)})
        return ["batch_mb_1"]

    monkeypatch.setattr("core.batch.submit_batches", submit_batches)
    payer = Payer(managed_board_id=4242)
    hook = batching.batch_event_hook(876543, "managed_board", "gpt-6-luna", payer=payer)
    with pytest.raises(batching.AwaitingBatch):
        await batching.submit_or_collect(
            876543,
            [batch.BatchSpec("a", "text")],
            "gpt-6-luna",
            "medium",
            6000,
            hook,
            fallback=custom_filter_shape("gpt-6-luna", "medium", "managed_board"),
            purpose="managed_board",
            payer=payer,
        )
    assert sent == ["gpt-6-luna", "claude-haiku-5-5"], "sonnet is outside the group"
    row = db.query_one(
        "SELECT model, fallback_from, purpose, payer, payer_id FROM ai_batches "
        "WHERE provider_batch_id = 'batch_mb_1'"
    )
    assert row == {
        "model": "claude-haiku-5-5",
        "fallback_from": "gpt-6-luna",
        "purpose": "managed_board",
        "payer": "managed_board",
        "payer_id": 4242,
    }


@pytest.mark.asyncio
async def test_without_a_chain_a_refusal_still_fails_with_its_reason_and_raises_the_alert(
    monkeypatch,
):
    from tasks.runtime import batching

    monkeypatch.setattr(batching, "has_batch_work", lambda _task_id: False)
    monkeypatch.setattr(batching, "snapshot_specs", lambda _task_id, specs: specs)

    async def submit_batches(specs, *_args, **_kwargs):
        raise ProviderExhausted("openai", "Billing hard limit has been reached", [], specs)

    monkeypatch.setattr("core.batch.submit_batches", submit_batches)
    with pytest.raises(RuntimeError, match="accepted by the provider: Billing hard limit"):
        await batching.submit_or_collect(
            1, [batch.BatchSpec("a", "text")], "gpt-6-luna", "low", 1500, None, fallback=COMP_TASK
        )
    alert = _alert("openai")
    assert alert and alert["resolved_at"] is None


def test_routing_moves_to_the_chain_only_while_the_provider_is_exhausted(set_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    set_config("model_fallback_chains", {"comp": ["claude-haiku-5-5"]})
    assert model_fallback.route(COMP_TASK).model == "gpt-6-luna"
    model_fallback.mark("openai", "Billing hard limit has been reached", model=None, purpose=None)
    rerouted = model_fallback.route(COMP_TASK)
    assert (rerouted.model, rerouted.fallback_from) == ("claude-haiku-5-5", "gpt-6-luna")
    model_fallback.clear("openai")
    assert model_fallback.route(COMP_TASK).model == "gpt-6-luna"
    assert _alert("openai")["resolved_at"] is not None


def test_an_exhausted_provider_is_tried_again_after_one_scheduler_cycle(set_config):
    model_fallback.mark("openai", "Billing hard limit has been reached", model=None, purpose=None)
    assert "openai" in model_fallback.blocked_providers()
    db.execute(
        "UPDATE health_alerts SET last_seen = now() - interval '61 minutes' "
        "WHERE kind = 'provider_exhausted' AND subject = 'openai' AND resolved_at IS NULL"
    )
    assert "openai" not in model_fallback.blocked_providers()
    assert [r["provider"] for r in model_fallback.exhausted()] == ["openai"], "still alerting"


def test_accepted_batches_whose_requests_were_refused_mark_the_provider():
    from tasks.runtime import batching

    refused = batch.BatchResult("u1", error="400: Billing hard limit has been reached")
    refused.model = "gpt-6-luna"
    other = batch.BatchResult("u2", error="400: reasoning.effort minimal is not supported")
    other.model = "gpt-6-luna"
    batching._mark_exhausted_results([other])
    assert _alert("openai") is None
    batching._mark_exhausted_results([other, refused])
    assert _alert("openai")["resolved_at"] is None


def test_the_admin_view_lists_every_purpose_its_chain_and_the_exhaustion(
    client, admin_headers, set_config
):
    set_config(
        "model_fallback_chains",
        {"verify": ["claude-haiku-5-5", "grok-4.3"], "managed_board": ["claude-sonnet-5"]},
    )
    set_config("verdict_model_equivalence", LUNA_HAIKU)
    model_fallback.mark("openai", "Billing hard limit has been reached", model=None, purpose=None)
    r = client.get("/v1/admin/model-fallback", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["provider"] for p in body["providers"]] == ["openai"]
    purposes = {p["purpose"]: p for p in body["purposes"]}
    assert "filter" in purposes and "managed_board" in purposes
    assert "job_profile" not in purposes
    haiku, grok = purposes["verify"]["chain"]
    assert haiku["rejection"] == "" and haiku["falls_back_for"] == ["gpt-6-luna"]
    assert "batch transport" in grok["rejection"] and grok["falls_back_for"] == []
    [sonnet] = purposes["managed_board"]["chain"]
    assert sonnet["falls_back_for"] == [], "grouped with nothing, so it never runs"
    assert purposes["comp"]["chain"] == []
