"""Emptying the copies answers carried, ahead of their drop: only where
ledger_rows shows the same value without them."""

from __future__ import annotations

import asyncio

from api import db
from core.filters import build_custom_input
from tasks import answer_copies
from tests.factories import legacy_answer

PAGE = "a posting body that is long enough to be a page " * 10
LEDGER = (
    "input_content, prompt_tokens, completion_tokens, total_tokens, cached_tokens, "
    "cache_write_tokens, reasoning_tokens, duration_ms, cost_usd"
)


def _run(f) -> dict:
    task_id = f.make_task("clear_answer_copies", status="running")
    asyncio.run(answer_copies.handle_clear_answer_copies(task_id, {}))
    return db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]


def _ledger(ids: list[int]) -> list[dict]:
    """What readers see, once tasks.answer_links has linked what it can."""
    from tasks import answer_links

    answer_links.link_range(0, 10**9)
    return db.query(f"SELECT {LEDGER} FROM ledger_rows WHERE id = ANY(%s) ORDER BY id", (ids,))


def _call(**columns) -> int:
    row = {
        "purpose": "verify",
        "model": "gpt-5-nano",
        "batched": True,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "cached_tokens": 0,
        "cost_usd": 0.0001,
        **columns,
    }
    names, values = ", ".join(row), ", ".join(f"%({k})s" for k in row)
    return db.query_one(f"INSERT INTO model_calls ({names}) VALUES ({values}) RETURNING id", row)[
        "id"
    ]


def test_copies_are_emptied_only_where_the_ledger_reads_the_same_without_them(f):
    url = "https://copies.test/a"
    f.make_fetch(url, content=PAGE)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "cached_tokens": 0,
        "cost_usd": 0.0001,
    }
    _call(provider_batch_id="b1", custom_id=url)
    custom = legacy_answer(
        url,
        "passed",
        check_type="custom",
        company="Acme",
        job_title="Engineer",
        config_name="filter-batch",
        batch_id="b1",
        input_content=build_custom_input("Acme", "Engineer", PAGE),
        **usage,
    )
    sibling = legacy_answer(
        url,
        "passed",
        check_type="closed",
        batch_id="b1",
        input_content="",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost_usd=0,
    )
    unpaid = legacy_answer(url, "passed", check_type="closed", input_content="")
    # A copy no fetch rebuilds and usage no call matches keep what they hold.
    stray = legacy_answer(
        "https://copies.test/b",
        "passed",
        check_type="custom",
        company="Acme",
        job_title="Engineer",
        input_content="Company: Someone Else\n...",
        total_tokens=7,
    )
    db.execute("UPDATE model_calls SET created_at = now() - interval '1 hour'")
    ids = [custom, sibling, unpaid, stray]
    before = _ledger(ids)
    progress = _run(f)

    # An empty copy was no copy: it reads as none.
    expected = [r | {"input_content": r["input_content"] or None} for r in before]
    assert _ledger(ids) == expected, "the ledger reads the same without the copies"
    held = db.query(
        "SELECT id FROM ai_queries WHERE input_content IS NOT NULL OR total_tokens IS NOT NULL"
    )
    assert held == [{"id": stray}]
    assert progress["left"] == {"inputs": 1, "usage": 1, "instructions": 0}
    assert _run(f)["total"] == 0, "a second run clears nothing"


def test_the_worker_stops_queueing_once_a_run_cleared_nothing(f):
    from api import worker

    def queued() -> int:
        return db.query_one(
            "SELECT count(*) AS n FROM tasks WHERE kind = 'clear_answer_copies' "
            "AND status = 'pending'"
        )["n"]

    worker.schedule_ingest_cycle()
    assert queued() == 1
    db.execute(
        "UPDATE tasks SET status = 'done', progress = '{\"total\": 0}'::jsonb "
        "WHERE kind = 'clear_answer_copies'"
    )
    worker.schedule_ingest_cycle()
    assert queued() == 0


def test_a_number_only_the_answer_kept_moves_to_its_call_before_it_is_emptied(f):
    """A live call the old usage ledger booked kept no reasoning tokens; its
    verdict did (one explain call on 2026-10-10). Emptying the copy must not
    lose them."""
    call = _call(batched=False, reasoning_tokens=None)
    answer = legacy_answer(
        "https://copies.test/r",
        "passed",
        check_type="custom",
        model="gpt-5-nano",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        cached_tokens=0,
        reasoning_tokens=150,
        duration_ms=900,
        cost_usd=0.0001,
    )
    db.execute("UPDATE ai_queries SET model_call_id = %s WHERE id = %s", (call, answer))

    _run(f)

    assert db.query_one(
        "SELECT reasoning_tokens, duration_ms FROM model_calls WHERE id = %s", (call,)
    ) == {"reasoning_tokens": 150, "duration_ms": 900}
    assert _ledger([answer])[0]["reasoning_tokens"] == 150
    held = db.query_one("SELECT reasoning_tokens FROM ai_queries WHERE id = %s", (answer,))
    assert held["reasoning_tokens"] is None, "emptied once the call holds it"
