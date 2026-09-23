from decimal import Decimal

import pytest

from api import db
from core import batch, pricing
from tasks import runtime


def test_batch_without_usage_keeps_cache_write_unknown():
    events = []
    batch._emit_usage(
        lambda *event: events.append(event),
        "empty",
        "completed",
        {"request": batch.BatchResult("request", usage=None)},
    )
    assert events[0][2]["cache_write_tokens"] is None


def test_batch_mixed_usage_keeps_cache_write_unknown():
    events = []
    batch._emit_usage(
        lambda *event: events.append(event),
        "mixed",
        "completed",
        {
            "known": batch.BatchResult(
                "known",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "input_tokens_details": {
                        "cached_tokens": 0,
                        "cache_write_tokens": 50,
                    },
                },
            ),
            "missing": batch.BatchResult("missing", usage=None),
        },
    )
    assert events[0][2]["cache_write_tokens"] is None


@pytest.mark.parametrize("model", ["gpt-5-mini", "grok-4.3", "unknown-model"])
def test_fleet_batch_preserves_cache_and_prices_each_request(f, model):
    task_id = f.make_task("extract_comp", {})
    hook = runtime.batch_event_hook(task_id, "comp", model)
    hook("paid", "submitted", {"requests": 2})
    usage = {
        "input_tokens": 200_000,
        "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 100_000, "cache_write_tokens": 50_000},
    }
    results = {str(i): batch.BatchResult(str(i), usage=usage, batch_id="paid") for i in range(2)}
    batch._emit_usage(hook, "paid", "completed", results)
    row = db.query_one(
        "SELECT prompt_tokens,completion_tokens,cached_tokens,cache_write_tokens,cost_usd FROM api_usage"
    )
    assert row["cached_tokens"] == 200_000
    assert row["cache_write_tokens"] == 100_000
    assert (row["prompt_tokens"], row["completion_tokens"]) == (400_000, 200)
    per_request = pricing.estimate_cost_usd(
        model,
        200_000,
        100,
        cached_tokens=100_000,
        cache_write_tokens=50_000,
        batched=True,
    )
    expected = (per_request * 2).quantize(Decimal("0.000001")) if per_request is not None else None
    assert row["cost_usd"] == expected
    assert db.query_one("SELECT est_cost_usd FROM ai_batches")["est_cost_usd"] == expected
    batch._emit_usage(hook, "paid", "completed", results)
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 1


def test_aggregate_only_tiered_batch_is_unpriced(f):
    task_id = f.make_task("extract_comp", {})
    hook = runtime.batch_event_hook(task_id, "comp", "grok-4.3")
    hook("legacy", "submitted", {"requests": 2})
    hook("legacy", "completed", {"input_tokens": 400_000, "output_tokens": 200})
    assert db.query_one("SELECT cost_usd FROM api_usage")["cost_usd"] is None
    assert db.query_one("SELECT est_cost_usd FROM ai_batches")["est_cost_usd"] is None


def test_replay_with_new_cache_write_metadata_does_not_recharge(f):
    task_id = f.make_task("extract_comp", {})
    hook = runtime.batch_event_hook(task_id, "comp", "gpt-5.6-luna")
    hook("old-image", "submitted", {"requests": 1})
    hook(
        "old-image",
        "completed",
        {"input_tokens": 1000, "output_tokens": 10, "cache_write_tokens": None},
    )
    before = db.query_one(
        "SELECT input_tokens, output_tokens, cache_write_tokens, est_cost_usd "
        "FROM ai_batches WHERE provider_batch_id = 'old-image'"
    )
    assert before == {
        "input_tokens": 1000,
        "output_tokens": 10,
        "cache_write_tokens": None,
        "est_cost_usd": None,
    }
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 1

    # A newer image has the same provider snapshot plus the write count. It
    # may enrich ai_batches, but it must not create another spend row or
    # replace the historical estimate.
    hook(
        "old-image",
        "completed",
        {"input_tokens": 1000, "output_tokens": 10, "cache_write_tokens": 400},
    )
    after = db.query_one(
        "SELECT input_tokens, output_tokens, cache_write_tokens, est_cost_usd "
        "FROM ai_batches WHERE provider_batch_id = 'old-image'"
    )
    assert after == {
        "input_tokens": 1000,
        "output_tokens": 10,
        "cache_write_tokens": 400,
        "est_cost_usd": None,
    }
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 1


def test_tier_selection_is_per_request_even_without_cache(f):
    task_id = f.make_task("extract_comp", {})
    hook = runtime.batch_event_hook(task_id, "comp", "grok-4.3")
    hook("tiered", "submitted", {"requests": 2})
    results = {
        str(i): batch.BatchResult(str(i), usage={"input_tokens": 199_999, "output_tokens": 100})
        for i in range(2)
    }
    batch._emit_usage(hook, "tiered", "completed", results)
    per_request = pricing.estimate_cost_usd("grok-4.3", 199_999, 100, batched=True)
    assert per_request is not None
    expected = (per_request * 2).quantize(Decimal("0.000001"))
    assert expected != pricing.estimate_cost_usd("grok-4.3", 399_998, 200, batched=True)
    assert db.query_one("SELECT cost_usd FROM api_usage")["cost_usd"] == expected
