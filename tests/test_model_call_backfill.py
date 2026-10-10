"""The call ledger's backfill: each era from the record that was right in it."""

import asyncio
from decimal import Decimal

from api import db, model_calls
from core import pricing
from core.store import add_ai_results, ai_result_row
from tasks import model_call_backfill

MODEL = "gpt-5-mini"
OLD = "now() - interval '3 days'"


def _batch(task_id, bid, purpose, *, requests, tokens=(0, 0), cost=None, recent=False):
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model, requests, completed, "
        "input_tokens, output_tokens, est_cost_usd, status, completed_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'completed', "
        f"{'now()' if recent else OLD})",
        (bid, task_id, purpose, MODEL, requests, requests, tokens[0], tokens[1], cost),
    )


def _verdict(url, check, bid, *, filter_name=None, tokens=True, config="worker") -> int:
    usage = (
        {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100, "cached_tokens": 0}
        if tokens
        else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
    )
    (qid,) = add_ai_results(
        [
            ai_result_row(
                url,
                "passed",
                check_type=check,
                model=MODEL,
                filter_name=filter_name,
                batch_id=bid,
                config_name=config,
                cache_write_tokens=0,
                **usage,
            )
        ]
    )
    return qid


def _receipt(task_id, bid, custom_id, tokens=(1000, 100)):
    usage = {
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "total_tokens": sum(tokens),
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    }
    db.execute(
        "INSERT INTO batch_result_receipts (provider_batch_id, custom_id, task_id, response, model) "
        "VALUES (%s, %s, %s, %s, %s)",
        (bid, custom_id, task_id, db.jsonb({"text": "{}", "usage": usage}), MODEL),
    )


def _item_cost(tokens=(1000, 100)) -> Decimal:
    cost = pricing.estimate_cost_usd(
        MODEL, tokens[0], tokens[1], cached_tokens=0, cache_write_tokens=0, batched=True
    )
    assert cost is not None
    return cost.quantize(Decimal("0.000001"))


def _run(f) -> dict:
    task_id = f.make_task("backfill_model_calls", status="running")
    asyncio.run(model_call_backfill.handle_backfill_model_calls(task_id, {}))
    return db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]


def _rows(**where) -> list[dict]:
    clause = " AND ".join(f"{k} = %({k})s" for k in where) or "true"
    return db.query(f"SELECT * FROM model_calls WHERE {clause} ORDER BY id", where)


def test_a_paid_verdict_is_one_call_and_its_siblings_are_none(f):
    user = f.make_user()
    task = f.make_task("run_filter_batch_chunk", {"user_id": user})
    _batch(task, "b-filter", "filter", requests=1, tokens=(1000, 100))
    paid = _verdict("https://x.test/1", "custom", "b-filter", filter_name=f"user{user}:mine")
    _verdict("https://x.test/1", "clearance", "b-filter", tokens=False)
    _run(f)
    (row,) = _rows()
    recorded = db.query_one("SELECT cost_usd FROM ai_queries WHERE id = %s", (paid,))
    assert (row["source"], row["source_id"], row["custom_id"]) == (
        "verdict",
        paid,
        "https://x.test/1",
    )
    assert (row["payer"], row["user_id"], row["key_source"]) == ("user", user, "owner")
    assert row["cost_usd"] == recorded["cost_usd"], "copied as recorded, not repriced"


def test_a_verify_batch_is_the_fleets_whoever_asked_the_question(f):
    task = f.make_task("verify_new", {})
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model) VALUES ('b', 'b', %s, 'p', 'h', %s) RETURNING id",
        (f.make_user(), MODEL),
    )["id"]
    _batch(task, "b-verify", "verify", requests=1, tokens=(1000, 100))
    _verdict("https://x.test/v", "custom", "b-verify", filter_name=f"managed-board:{board}")
    _run(f)
    (row,) = _rows()
    assert (row["payer"], row["managed_board_id"], row["key_source"]) == ("fleet", None, "server")


def test_a_billed_item_that_wrote_no_verdict_comes_from_its_receipt(f):
    task = f.make_task("verify_new", {})
    _batch(task, "b-v", "verify", requests=2, tokens=(2000, 200))
    _verdict("https://x.test/a", "closed", "b-v")
    _receipt(task, "b-v", "https://x.test/a")
    _receipt(task, "b-v", "https://x.test/failed")
    _run(f)
    rows = _rows()
    assert [(r["source"], r["custom_id"]) for r in rows] == [
        ("verdict", "https://x.test/a"),
        ("receipt", "https://x.test/failed"),
    ]
    assert rows[1]["cost_usd"] == _item_cost()


def test_receipts_are_items_only_where_they_price_to_what_the_batch_recorded(f):
    task = f.make_task("extract_comp", {})
    _batch(task, "b-same", "comp", requests=2, tokens=(2000, 200), cost=2 * _item_cost())
    _batch(task, "b-repriced", "comp", requests=2, tokens=(2000, 200), cost=Decimal("0.5"))
    for bid in ("b-same", "b-repriced"):
        _receipt(task, bid, "1")
        _receipt(task, bid, "2")
    _run(f)
    items = _rows(provider_batch_id="b-same")
    assert [(r["source"], r["custom_id"], r["requests"]) for r in items] == [
        ("receipt", "1", 1),
        ("receipt", "2", 1),
    ]
    (whole,) = _rows(provider_batch_id="b-repriced")
    assert (whole["source"], whole["custom_id"], whole["requests"]) == ("batch", None, 2)
    assert whole["cost_usd"] == Decimal("0.5"), "the cost the batch recorded"
    assert (whole["prompt_tokens"], whole["completion_tokens"]) == (2000, 200)


def test_a_batch_without_receipts_is_its_totals_less_its_verdicts(f):
    task = f.make_task("verify_new", {})
    _batch(task, "b-old", "verify", requests=3, tokens=(3000, 300), cost=Decimal("0.009"))
    paid = _verdict("https://x.test/o", "closed", "b-old")
    _batch(task, "b-mail", "mail_classify", requests=5, tokens=(500, 50), cost=Decimal("0.002"))
    _batch(task, "b-fresh", "mail_classify", requests=1, tokens=(100, 10), recent=True)
    _run(f)
    verdict_cost = db.query_one("SELECT cost_usd FROM ai_queries WHERE id = %s", (paid,))[
        "cost_usd"
    ]
    rest = _rows(provider_batch_id="b-old", source="batch")[0]
    assert (rest["requests"], rest["prompt_tokens"], rest["completion_tokens"]) == (2, 2000, 200)
    assert rest["cost_usd"] == Decimal("0.009") - verdict_cost
    (mail,) = _rows(provider_batch_id="b-mail")
    assert (mail["payer"], mail["requests"], mail["cost_usd"]) == ("fleet", 5, Decimal("0.002"))
    assert _rows(provider_batch_id="b-fresh") == [], "its receipts may still be on their way"


def test_live_calls_are_copied_only_from_before_the_writer_started(f):
    user = f.make_user()
    before = _verdict("https://x.test/l", "custom", None, filter_name=f"user{user}:f")
    explained = _verdict("https://x.test/e", "custom", None, config="explain")
    db.execute(
        f"UPDATE ai_queries SET created_at = {OLD} WHERE id IN (%s, %s)", (before, explained)
    )
    for purpose, batched in (("application", False), ("application", True), ("filter", False)):
        db.execute(
            "INSERT INTO api_usage (user_id, key_source, purpose, model, prompt_tokens, "
            "completion_tokens, total_tokens, batched, cost_usd, created_at) "
            f"VALUES (%s, 'owner', %s, %s, 10, 1, 11, %s, 0.000010, {OLD})",
            (user, purpose, MODEL, batched),
        )
    # The first call the ledger itself recorded is the cutover.
    model_calls.record(
        [
            model_calls.Call(
                "explain",
                MODEL,
                model_calls.Payer(user_id=user),
                "owner",
                {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            )
        ]
    )
    after = _verdict("https://x.test/later", "custom", None, filter_name=f"user{user}:f")
    _run(f)
    copied = {(r["source"], r["purpose"], r["source_id"]) for r in _rows() if r["source"] != "call"}
    usage_id = db.query_one(
        "SELECT id FROM api_usage WHERE purpose = 'application' AND NOT batched"
    )["id"]
    assert copied == {("verdict", "filter", before), ("usage", "application", usage_id)}
    assert after not in {r["source_id"] for r in _rows()}


def test_a_second_run_writes_nothing(f):
    task = f.make_task("verify_new", {})
    _batch(task, "b-v", "verify", requests=2, tokens=(2000, 200), cost=Decimal("0.01"))
    _verdict("https://x.test/a", "closed", "b-v")
    _receipt(task, "b-v", "https://x.test/a")
    _receipt(task, "b-v", "https://x.test/b")
    _batch(task, "b-old", "comp", requests=4, tokens=(400, 40), cost=Decimal("0.001"))
    _run(f)
    first = len(_rows())
    progress = _run(f)
    assert len(_rows()) == first == 3
    assert set(progress["written"].values()) == {0}
