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


def _scenario(write) -> None:
    """One joint batched call, one live call, one unpriced call, one superseded
    answer: every cut /admin/spend makes."""
    big = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "total_tokens": 2_000_000}
    write("https://s.test/a", "passed", "closed", NANO, big, "batch-1")
    write("https://s.test/a", "passed", "clearance", NANO, {}, "batch-1")
    write(
        "https://s.test/b",
        "rejected",
        "custom",
        NANO,
        {
            "prompt_tokens": 1000,
            "completion_tokens": 10,
            "total_tokens": 1010,
            "cached_tokens": 800,
        },
        None,
    )
    write("https://s.test/c", "passed", "closed", "some-new-model", big, None)
    write("https://s.test/c", "rejected", "closed", NANO, big, None)


def _legacy(url, status, check_type, model, usage, batch_id) -> None:
    # A sibling as Verdict(shared_call=True) wrote it: explicit zeros.
    sibling = dict.fromkeys(
        (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ),
        0,
    )
    legacy_answer(
        url, status, check_type=check_type, model=model, batch_id=batch_id, **(usage or sibling)
    )


def _paid(url, status, check_type, model, usage, batch_id) -> None:
    paid_answer(url, status, check_type=check_type, model=model, usage=usage, batch_id=batch_id)


def test_spend_reads_the_same_numbers_from_calls_as_from_the_copies(client, admin_headers):
    """The verdict diagnostics read ledger_rows. The same answers, stored as
    copies and as pointers, give the same page."""

    def spend() -> dict:
        body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
        diagnostics = body["verdict_diagnostics"]
        for key in ("window",):
            body.pop(key, None)
        return {
            key: diagnostics[key]
            for key in ("totals", "batching", "by_check_type", "by_model", "by_reach", "waste")
        }

    _scenario(_legacy)
    copies = spend()
    db.execute("DELETE FROM ai_queries")
    db.execute("DELETE FROM model_calls")
    _scenario(_paid)
    pointers = spend()

    def stripped(page: dict) -> dict:
        # first_call and last_call are when the rows were written.
        for cut in ("totals",):
            page[cut] = {k: v for k, v in page[cut].items() if k not in ("first_call", "last_call")}
        return page

    assert stripped(pointers) == stripped(copies)
    assert copies["totals"]["calls"] == 5 and copies["totals"]["unpriced_calls"] == 1
