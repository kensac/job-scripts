from decimal import Decimal

import pytest

from api import db
from api.tasks import runtime
from core import batch, pricing


@pytest.mark.parametrize("model", ["gpt-5-mini", "grok-4.3", "unknown-model"])
def test_fleet_batch_preserves_cache_and_prices_each_request(f, model):
    task_id = f.make_task("extract_comp", {})
    hook = runtime.batch_event_hook(task_id, "comp", model)
    hook("paid", "submitted", {"requests": 2})
    usage = {
        "input_tokens": 200_000,
        "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 100_000},
    }
    results = {str(i): batch.BatchResult(str(i), usage=usage, batch_id="paid") for i in range(2)}
    batch._emit_usage(hook, "paid", "completed", results)
    row = db.query_one(
        "SELECT prompt_tokens,completion_tokens,cached_tokens,cost_usd FROM api_usage"
    )
    assert row["cached_tokens"] == 200_000
    assert (row["prompt_tokens"], row["completion_tokens"]) == (400_000, 200)
    per_request = pricing.estimate_cost_usd(
        model, 200_000, 100, cached_tokens=100_000, batched=True
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


def test_tier_selection_is_per_request_even_without_cache(f):
    task_id = f.make_task("extract_comp", {})
    hook = runtime.batch_event_hook(task_id, "comp", "grok-4.3")
    hook("tiered", "submitted", {"requests": 2})
    results = {
        str(i): batch.BatchResult(str(i), usage={"input_tokens": 200_000, "output_tokens": 100})
        for i in range(2)
    }
    batch._emit_usage(hook, "tiered", "completed", results)
    per_request = pricing.estimate_cost_usd("grok-4.3", 200_000, 100, batched=True)
    assert per_request is not None
    expected = (per_request * 2).quantize(Decimal("0.000001"))
    assert expected != pricing.estimate_cost_usd("grok-4.3", 400_000, 200, batched=True)
    assert db.query_one("SELECT cost_usd FROM api_usage")["cost_usd"] == expected
