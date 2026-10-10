"""The call ledger: one row per paid provider request, from one writer."""

import pathlib
from decimal import ROUND_HALF_UP, Decimal

from api import budget, db, model_calls
from api.ai import batch_results
from api.model_calls import FLEET, Payer
from core import batch, pricing
from tasks import runtime

MODEL = "gpt-5-mini"


def _usage(i: int) -> dict:
    return {
        "input_tokens": 1_000 + i,
        "output_tokens": 50 + i,
        "total_tokens": 1_050 + 2 * i,
        "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 100},
        "output_tokens_details": {"reasoning_tokens": 20},
    }


def _collect(f, payer: Payer) -> tuple[int, list]:
    task_id = f.make_task("run_filter_batch", {})
    hook = runtime.batch_event_hook(task_id, "filter", MODEL, payer=payer)
    hook("b-1", "submitted", {"requests": 4})
    results = [
        batch.BatchResult(f"item-{i}", text="{}", usage=_usage(i), batch_id="b-1") for i in range(3)
    ]
    # A request the provider failed and did not bill.
    results.append(batch.BatchResult("item-failed", error="server_error", batch_id="b-1"))
    batch_results.checkpoint(task_id, results, [])
    return task_id, results


def _unrecorded_batch(f) -> int:
    """A batch submitted by an image that did not record payers."""
    task_id = f.make_task("run_filter_batch", {})
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model) "
        "VALUES ('b-1', %s, 'filter', %s)",
        (task_id, MODEL),
    )
    return task_id


def _calls() -> list[dict]:
    return db.query("SELECT * FROM model_calls ORDER BY custom_id")


def test_each_collected_item_is_one_call_priced_on_its_own(f):
    user = f.make_user()
    task_id, results = _collect(f, Payer(user_id=user))
    calls = _calls()
    assert [c["custom_id"] for c in calls] == ["item-0", "item-1", "item-2"]
    assert {(c["user_id"], c["managed_board_id"], c["key_source"]) for c in calls} == {
        (user, None, "owner")
    }
    assert {(c["purpose"], c["model"], c["task_id"]) for c in calls} == {("filter", MODEL, task_id)}
    # Each request priced on its own tokens, at the batch rate: a tiered
    # model's tier is chosen per request, never from a batch's sum.
    for i, call in enumerate(calls):
        u = _usage(i)
        assert call["cost_usd"] == pricing.estimate_cost_usd(
            MODEL,
            u["input_tokens"],
            u["output_tokens"],
            cached_tokens=200,
            cache_write_tokens=100,
            batched=True,
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    assert [c["cache_write_tokens"] for c in calls] == [100, 100, 100]
    assert [c["reasoning_tokens"] for c in calls] == [20, 20, 20]

    batch_results.checkpoint(task_id, results, [])
    assert len(_calls()) == 3, "a replayed receipt is not a second call"


def test_fleet_items_are_on_the_server_key(f):
    _collect(f, FLEET)
    assert {(c["user_id"], c["managed_board_id"], c["key_source"]) for c in _calls()} == {
        (None, None, "server")
    }


def test_a_board_pays_for_its_own_batch(f):
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model) VALUES ('ledger', 'ledger', %s, 'p', 'h', %s) RETURNING id",
        (f.make_user(), MODEL),
    )["id"]
    _collect(f, Payer(managed_board_id=board))
    assert {(c["user_id"], c["managed_board_id"]) for c in _calls()} == {(None, board)}


def test_a_batch_with_no_recorded_payer_writes_no_call(f):
    """Its calls are the backfill's, not a guess at who paid."""
    task_id = _unrecorded_batch(f)
    batch_results.checkpoint(
        task_id, [batch.BatchResult("item-0", usage=_usage(0), batch_id="b-1")], []
    )
    assert _calls() == []


def test_the_task_collecting_an_unrecorded_batch_records_its_payer(f):
    """The collector is the task that submitted it, so it knows who pays."""
    user = f.make_user()
    task_id = _unrecorded_batch(f)
    hook = runtime.batch_event_hook(task_id, "filter", MODEL, payer=Payer(user_id=user))
    hook("b-1", "in_progress", {"requests": 1})
    hook("b-1", "in_progress", {"requests": 1})
    row = db.query_one("SELECT payer, payer_id FROM ai_batches")
    assert (row["payer"], row["payer_id"]) == ("user", user)
    other = runtime.batch_event_hook(task_id, "filter", MODEL)
    other("b-1", "in_progress", {"requests": 1})
    row = db.query_one("SELECT payer, payer_id FROM ai_batches")
    assert (row["payer"], row["payer_id"]) == ("user", user), "a recorded payer never changes"


def test_live_calls_are_recorded_and_batched_bookings_are_not(f):
    user = f.make_user()
    usage = {
        "prompt_tokens": 500,
        "completion_tokens": 40,
        "total_tokens": 540,
        "cached_tokens": 0,
        "cache_write_tokens": None,
        "reasoning_tokens": 10,
    }
    budget.record_tokens(user, "byo", "explain", MODEL, usage)
    budget.record_tokens(user, "owner", "filter", MODEL, usage, batched=True)
    calls = _calls()
    assert [(c["purpose"], c["key_source"], c["provider_batch_id"]) for c in calls] == [
        ("explain", "byo", None)
    ]
    assert calls[0]["reasoning_tokens"] == 10
    assert calls[0]["cost_usd"] == pricing.estimate_cost_usd(
        MODEL, 500, 40, cached_tokens=0, cache_write_tokens=None
    )


def test_a_call_with_no_tokens_is_not_a_row(f):
    model_calls.record([model_calls.Call("explain", MODEL, FLEET, "server", {})])
    assert _calls() == []


def test_model_calls_has_one_writer():
    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    writers = [
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "INSERT INTO model_calls" in p.read_text()
    ]
    assert writers == ["api/model_calls.py"]
