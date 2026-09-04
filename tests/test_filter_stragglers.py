"""A filter run parked on a straggler batch must not hold a user's board.

After #328 the parked chunks collected their finished batches within minutes,
and the board still did not move: 91 passes were recorded but the parent
materializes only when its slowest chunk finishes, and no new run could be
scheduled for the user while that parent was 'waiting'. Two rules fix it:
each chunk publishes its own passes, and a split run no longer blocks the
next because the splitter excludes every url a live chunk still holds.
"""

from __future__ import annotations

import pytest

from api import ai, db, fetching
from api.tasks import filters as tasks_filters
from api.tasks import ingest as tasks_ingest
from api.tasks.board import _in_flight_urls
from core.store import add_ai_result
from tests.factories import make_task


def _user_id() -> int:
    row = db.query_one("SELECT id, groups FROM users WHERE sub = 'test-user'")
    assert row is not None
    return row["id"]


def _entitle(user_id: int) -> None:
    """Puts the user on the owner key through a group budget, which is what
    schedule_filter_runs selects on."""
    row = db.query_one("SELECT groups FROM users WHERE id = %s", (user_id,))
    assert row is not None and row["groups"], "test user needs a group"
    db.execute(
        "INSERT INTO group_budgets (group_name) VALUES (%s) ON CONFLICT DO NOTHING",
        (row["groups"][0],),
    )
    db.execute("INSERT INTO user_sources (user_id, source) VALUES (%s, 'internships')", (user_id,))
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) VALUES (%s, %s, %s, %s)",
        (user_id, "f1", "no crypto companies", "hash1"),
    )


def _runs(user_id: int) -> list[dict]:
    return db.query(
        "SELECT id, status FROM tasks WHERE kind = 'run_all_filters' "
        "AND (payload->>'user_id')::bigint = %s ORDER BY id",
        (user_id,),
    )


def test_in_flight_urls_are_what_live_chunks_hold():
    uid = 7
    make_task(
        "run_filter_batch_chunk",
        {"user_id": uid, "jobs": [{"url": "https://a"}, {"url": "https://b"}]},
        status="awaiting_batch",
    )
    make_task(
        "run_filter_chunk", {"user_id": uid, "jobs": [{"url": "https://c"}]}, status="pending"
    )
    make_task("run_filter_chunk", {"user_id": uid, "jobs": [{"url": "https://d"}]}, status="done")
    make_task("run_filter_chunk", {"user_id": 8, "jobs": [{"url": "https://e"}]}, status="running")
    assert _in_flight_urls(uid) == {"https://a", "https://b", "https://c"}


def test_a_waiting_run_no_longer_blocks_the_next_cycle(user_headers):
    uid = _user_id()
    _entitle(uid)
    make_task("run_all_filters", {"user_id": uid, "batched": True}, status="waiting")
    tasks_ingest.schedule_filter_runs("2026-09-04T16:00")
    assert [r["status"] for r in _runs(uid)] == ["waiting", "pending"]


def test_a_run_that_has_not_split_still_blocks(user_headers):
    uid = _user_id()
    _entitle(uid)
    make_task("run_all_filters", {"user_id": uid, "batched": True}, status="running")
    tasks_ingest.schedule_filter_runs("2026-09-04T16:00")
    assert [r["status"] for r in _runs(uid)] == ["running"]


@pytest.mark.asyncio
async def test_a_chunk_publishes_its_passes_before_the_parent_finishes(monkeypatch, user_headers):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def no_network(url):
        raise AssertionError(f"scrape attempted for {url}")

    async def fake_parse(cfg, instructions, input_text, response_model, timeout=120.0):
        return response_model(should_filter=False, reason="test"), {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }

    monkeypatch.setattr(fetching, "fetch_page", no_network)
    monkeypatch.setattr(ai, "parse", fake_parse)
    uid = _user_id()
    _entitle(uid)
    url = "https://jobs.example.com/job-1"
    db.execute(
        "INSERT INTO jobs (url, company, title, source) VALUES (%s, 'co', 'SWE', 'internships')",
        (url,),
    )
    add_ai_result(url, "passed", "content cached", "content", input_content="great job")
    add_ai_result(url, "passed", "not closed", "closed")
    parent = make_task("run_all_filters", {"user_id": uid, "batched": True}, status="waiting")
    flt = db.query_one("SELECT name, prompt, on_ambiguous, prompt_hash FROM user_filters")
    chunk = make_task(
        "run_filter_chunk",
        {
            "parent_id": parent,
            "user_id": uid,
            "filter": flt,
            "jobs": [{"url": url, "company": "co", "title": "SWE"}],
        },
        status="running",
    )
    payload = db.query_one("SELECT payload FROM tasks WHERE id = %s", (chunk,))["payload"]
    await tasks_filters.handle_run_filter_chunk(chunk, payload)

    # The parent is still waiting (nothing finalized it); the board has the row.
    assert db.query_one("SELECT status FROM tasks WHERE id = %s", (parent,))["status"] == "waiting"
    on_board = db.query_one(
        "SELECT 1 AS x FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id "
        "WHERE uj.user_id = %s AND j.url = %s",
        (uid, url),
    )
    assert on_board is not None
