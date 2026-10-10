"""The spend readers on model_calls give the numbers they gave on api_usage.

Each reference below is the reader's SQL as it was over api_usage. Calls go
in through the real writers, which still write both tables, so the two must
agree: exactly for a person and a board, and for the fleet in everything but
the count of calls (api_usage booked a fleet batch as one row; the ledger
books each request) and the rounding of one batch total against one per item.
"""

from decimal import Decimal

from api import budget, db, managed_board_runs, model_calls
from api.ai import batch_results
from api.model_calls import FLEET, Payer
from api.routers.spend import _ledger_breakdowns
from core import batch
from tasks import runtime

MODEL = "gpt-5-mini"


def _usage(i: int) -> dict:
    return {
        "input_tokens": 1_000 + i,
        "output_tokens": 50 + i,
        "total_tokens": 1_050 + 2 * i,
        "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 10},
        "output_tokens_details": {"reasoning_tokens": 5},
    }


def _live(i: int) -> dict:
    return {
        "prompt_tokens": 400 + i,
        "completion_tokens": 30,
        "total_tokens": 430 + i,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }


def _batch(f, purpose: str, payer: Payer, bid: str, n: int, book=None) -> None:
    task_id = f.make_task("run_filter_batch_chunk", {})
    hook = runtime.batch_event_hook(task_id, purpose, MODEL, payer=payer)
    hook(bid, "submitted", {"requests": n})
    results = [
        batch.BatchResult(f"{bid}-{i}", text="{}", usage=_usage(i), batch_id=bid) for i in range(n)
    ]
    batch._emit_usage(hook, bid, "completed", {r.custom_id: r for r in results})
    batch_results.checkpoint(task_id, results, [])
    for result in results:
        if book:
            # What the consumer of a person's or a board's receipt books.
            from api.ai import batch_usage

            book(batch_usage(result.usage))


def _world(f) -> tuple[int, int]:
    user = f.make_user()
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model) VALUES ('r', 'r', %s, 'p', 'h', %s) RETURNING id",
        (user, MODEL),
    )["id"]
    for i in range(3):
        budget.record_tokens(user, "owner", "filter", MODEL, _live(i))
        budget.record_tokens(user, "byo", "explain", MODEL, _live(i))
        budget.record_managed_board_tokens(board, "managed_board", MODEL, _live(i))
    _batch(
        f,
        "filter",
        Payer(user_id=user),
        "b-user",
        4,
        lambda u: budget.record_tokens(user, "owner", "filter", MODEL, u, batched=True),
    )
    _batch(
        f,
        "managed_board",
        Payer(managed_board_id=board),
        "b-board",
        3,
        lambda u: budget.record_managed_board_tokens(
            board, "managed_board", MODEL, u, batched=True
        ),
    )
    _batch(f, "comp", FLEET, "b-fleet", 5)
    return user, board


def _old_user_by_day(user: int) -> list[dict]:
    return db.query(
        """
        SELECT created_at::date AS day, key_source,
               SUM(total_tokens) AS tokens, COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
        FROM api_usage WHERE user_id = %s AND created_at > now() - interval '30 days'
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        (user,),
    )


def _old_user_by_purpose(user: int) -> list[dict]:
    return db.query(
        """
        SELECT purpose, model, SUM(total_tokens) AS tokens, COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_calls,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
        FROM api_usage WHERE user_id = %s GROUP BY 1, 2 ORDER BY 3 DESC
        """,
        (user,),
    )


def test_a_persons_spend_is_unchanged(f):
    user, _ = _world(f)
    old_spent = db.query_one(
        "SELECT COALESCE(SUM(total_tokens), 0) AS spent FROM api_usage "
        "WHERE user_id = %s AND key_source = 'owner' AND created_at >= now() - interval '7 days'",
        (user,),
    )["spent"]
    assert budget.spent_this_week(user) == old_spent > 0
    by_day = sorted(model_calls.user_spend_by_day(user), key=lambda r: (r["day"], r["key_source"]))
    assert by_day == _old_user_by_day(user)
    by_purpose = sorted(model_calls.user_spend_by_purpose(user), key=lambda r: r["purpose"])
    assert by_purpose == sorted(_old_user_by_purpose(user), key=lambda r: r["purpose"])


def test_a_boards_cost_and_its_sponsors_week_are_unchanged(f):
    _, board = _world(f)
    old = db.query_one(
        "SELECT count(*) AS calls, COALESCE(sum(total_tokens), 0) AS total_tokens, "
        "COALESCE(sum(cost_usd), 0) AS cost_usd FROM api_usage WHERE managed_board_id = %s",
        (board,),
    )
    new = managed_board_runs.cost(board)
    assert (new.calls, new.total_tokens) == (old["calls"], old["total_tokens"])
    assert Decimal(str(new.cost_usd)) == old["cost_usd"]
    assert new.week_calls == new.calls == 6


def test_the_fleet_differs_only_in_calls_and_rounding(f):
    _world(f)
    old_week = db.query_one(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM api_usage WHERE user_id IS NULL "
        "AND created_at >= date_trunc('week', now() AT TIME ZONE 'UTC')"
    )["spent"]
    # Board calls are not the fleet's ledger rows but count against its
    # ceiling, on both tables: 3 live and 3 batched board calls, 5 fleet items.
    assert abs(budget.fleet_spend_this_week() - old_week) <= Decimal("0.000001") * 11

    ledger = _ledger_breakdowns({"days": 30})
    old = db.query_one(
        "SELECT COUNT(*) AS rows, COALESCE(SUM(total_tokens), 0) AS tokens, "
        "COALESCE(SUM(cost_usd), 0) AS cost FROM api_usage"
    )
    assert ledger.totals.total_tokens == old["tokens"]
    # The fleet batch was one api_usage row and is five calls here.
    assert ledger.totals.calls == old["rows"] - 1 + 5
    assert ledger.totals.ledger_rows == ledger.totals.calls
    assert abs(Decimal(str(ledger.totals.cost_usd)) - old["cost"]) <= Decimal("0.000001") * 5
