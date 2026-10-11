"""ledger_rows reads an answer's input through its fetch and its usage
through its call, and shows what the stored copies showed."""

from __future__ import annotations

from decimal import Decimal

from api import db
from core.answers import VERIFY_INPUT_CHARS
from core.filters import build_custom_input
from core.store import add_ai_result
from tests.factories import legacy_answer, paid_answer

PAGE = "a posting body that is long enough to be a page " * 10
NANO = "gpt-5-nano"


def _row(answer_id: int) -> dict:
    return db.query_one(
        "SELECT input_content, prompt_tokens, completion_tokens, total_tokens, cached_tokens, "
        "cache_write_tokens, reasoning_tokens, duration_ms, cost_usd FROM ledger_rows "
        "WHERE id = %s",
        (answer_id,),
    )


def test_an_answer_shows_the_input_rebuilt_from_the_fetch_it_names(f):
    fetch = f.make_fetch("https://p.test/a", content=PAGE)
    custom = add_ai_result(
        "https://p.test/a",
        "passed",
        check_type="custom",
        company="Acme",
        job_title="Engineer",
        config_name="filter-batch",
        page_fetch_id=fetch,
    )
    long_page = "y" * (VERIFY_INPUT_CHARS + 10)
    long_fetch = f.make_fetch("https://p.test/b", content=long_page)
    board = add_ai_result(
        "https://p.test/b",
        "passed",
        check_type="custom",
        company="Acme",
        job_title="Engineer",
        config_name="verify-batch",
        page_fetch_id=long_fetch,
    )
    explained = add_ai_result(
        "https://p.test/a",
        "passed",
        check_type="explain:closed",
        config_name="explain",
        page_fetch_id=fetch,
    )
    closed = add_ai_result("https://p.test/a", "passed", check_type="closed", page_fetch_id=fetch)
    legacy = legacy_answer("https://p.test/c", "passed", check_type="closed", input_content="old")

    assert _row(custom)["input_content"] == build_custom_input("Acme", "Engineer", PAGE)
    assert _row(board)["input_content"] == build_custom_input(
        "Acme", "Engineer", long_page[:VERIFY_INPUT_CHARS]
    )
    assert _row(explained)["input_content"] == PAGE
    assert _row(closed)["input_content"] == PAGE
    assert _row(legacy)["input_content"] == "old", "an unlinked answer shows its copy"


def test_one_call_shows_on_its_first_answer_and_zeros_on_its_siblings(f):
    usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
    closed = paid_answer(
        "https://p.test/j", check_type="closed", model=NANO, usage=usage, batch_id="b1"
    )
    clearance = paid_answer(
        "https://p.test/j", check_type="clearance", model=NANO, usage={}, batch_id="b1"
    )
    call = db.query_one("SELECT cost_usd FROM model_calls")["cost_usd"]

    assert _row(closed) | {"input_content": None} == {
        "input_content": None,
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "total_tokens": 1500,
        "cached_tokens": 0,
        "cache_write_tokens": None,
        "reasoning_tokens": 0,
        "duration_ms": None,
        "cost_usd": call,
    }
    assert _row(clearance) | {"input_content": None} == {
        "input_content": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "duration_ms": None,
        "cost_usd": Decimal("0"),
    }


def test_a_sibling_of_an_unpriced_call_is_unpriced_too(f):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    paid_answer(
        "https://p.test/u", check_type="closed", model="unpriced", usage=usage, batch_id="b2"
    )
    sibling = paid_answer(
        "https://p.test/u", check_type="clearance", model="unpriced", usage={}, batch_id="b2"
    )
    assert _row(sibling)["cost_usd"] is None
