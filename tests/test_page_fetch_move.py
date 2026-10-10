"""Moving page fetches out of ai_queries, and the text copies older
verification kept only on its answers."""

from __future__ import annotations

import asyncio
import threading

from api import db, worker
from core import store
from core.pool import pool
from core.store import add_ai_result
from tasks import page_fetch_move

PAGE = "Posting text. " * 40
SHORT = "Sign in to continue."


def _fetches() -> list[dict]:
    return db.query(
        "SELECT id, url, status, method, content, worker FROM page_fetch_rows ORDER BY id"
    )


def _content_rows() -> int:
    return page_fetch_move.remaining()


def _run(f) -> dict:
    task_id = f.make_task("move_page_fetches", status="running")
    asyncio.run(page_fetch_move.handle_move_page_fetches(task_id, {}))
    return db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]


def _answers(urls: list[str]) -> dict:
    lateral = {
        r["url"]: r["input_content"]
        for r in db.query(
            "SELECT j.url, q.input_content FROM unnest(%s::text[]) AS j(url) "
            + store.CONTENT_LATERAL.format(url="j.url", columns="input_content"),
            (urls,),
        )
    }
    return {"lateral": lateral, "newest": store.get_contents(urls)}


def test_a_fetch_moves_with_its_id_and_every_field(f):
    passed = add_ai_result(
        "https://x/a", "passed", "ats text", check_type="content", input_content=PAGE
    )
    failed = add_ai_result("https://x/a", "failed", "fetch returned nothing", check_type="content")
    unlabelled = add_ai_result(
        "https://x/b", "passed", "content cached", check_type="content", input_content=PAGE
    )
    answer = add_ai_result("https://x/a", "passed", "job open", check_type="closed")
    before = db.query("SELECT * FROM page_fetches ORDER BY id")

    progress = _run(f)

    assert progress["moved"] == 3
    assert [(r["id"], r["method"]) for r in _fetches()] == [
        (passed, "ats text"),
        (failed, "fetch returned nothing"),
        (unlabelled, "unknown"),
    ]
    assert _content_rows() == 0
    after = db.query("SELECT * FROM page_fetches ORDER BY id")
    assert [{**r, "method": None} for r in after] == [{**r, "method": None} for r in before]
    assert db.query_one("SELECT 1 FROM ai_queries WHERE id = %s", (answer,))


def test_a_second_run_changes_nothing(f):
    add_ai_result("https://x/a", "passed", "ats text", check_type="content", input_content=PAGE)
    add_ai_result("https://x/c", "passed", "job open", check_type="closed", input_content=PAGE)
    _run(f)
    first = _fetches()

    progress = _run(f)

    assert progress["moved"] == 0 and progress["converted"] == 0
    assert _fetches() == first


def test_a_fetch_written_during_the_run_is_moved_by_it(f, monkeypatch):
    """An image that still writes fetches into ai_queries keeps writing while
    the move runs. A row that lands behind the cursor waits for the next run;
    one ahead of it is taken by this one."""
    first = add_ai_result(
        "https://x/a", "passed", "ats text", check_type="content", input_content=PAGE
    )
    late: list[int] = []
    real = page_fetch_move.move_batch

    def batch_then_write(after: int, limit: int = 1) -> list[int]:
        ids = real(after, 1)
        if not late:
            late.append(
                add_ai_result(
                    "https://x/b", "passed", "scraped", check_type="content", input_content=PAGE
                )
            )
        return ids

    monkeypatch.setattr(page_fetch_move, "move_batch", batch_then_write)
    _run(f)

    assert [r["id"] for r in _fetches()] == [first, late[0]]
    assert _content_rows() == 0


def test_a_row_another_writer_deletes_is_in_neither_table(f):
    """The move waits on a row another transaction holds. If that transaction
    deletes it, the move skips it rather than inserting a row with no source."""
    kept = add_ai_result(
        "https://x/a", "passed", "ats text", check_type="content", input_content=PAGE
    )
    gone = add_ai_result(
        "https://x/b", "passed", "ats text", check_type="content", input_content=PAGE
    )
    result: list[list[int]] = []
    with pool.connection() as holder:
        holder.execute("DELETE FROM ai_queries WHERE id = %s", (gone,))
        mover = threading.Thread(target=lambda: result.append(page_fetch_move.move_batch(0)))
        mover.start()
        mover.join(timeout=1)
        assert mover.is_alive(), "the move must wait on the held row, not pass it"
    mover.join(timeout=10)

    assert result == [[kept]]
    assert [r["id"] for r in _fetches()] == [kept]
    assert not db.query("SELECT 1 FROM ai_queries WHERE id = ANY(%s)", ([kept, gone],))


def test_a_url_whose_text_is_only_on_answers_gets_the_picked_copies_as_fetches(f):
    """Only the copies a reader picks become fetches, so no reader's text
    changes. The newest copy is short here, so the lateral picks an older one
    and both convert; the oldest copy stays a copy."""
    old = add_ai_result(
        "https://x/c", "passed", "job open", check_type="closed", input_content=PAGE
    )
    usable = add_ai_result(
        "https://x/c", "passed", "no restrictions", check_type="clearance", input_content=PAGE + "!"
    )
    newest = add_ai_result(
        "https://x/c", "passed", "job open", check_type="closed", input_content=SHORT
    )
    fetched = add_ai_result(
        "https://x/d", "passed", "ats text", check_type="content", input_content=PAGE
    )
    add_ai_result("https://x/d", "passed", "job open", check_type="closed", input_content=PAGE)
    add_ai_result("https://x/e", "passed", "x", check_type="custom", input_content="Acme\n" + PAGE)
    urls = ["https://x/c", "https://x/d", "https://x/e"]
    before = _answers(urls)
    ledger = "SELECT id, check_type FROM ledger_rows ORDER BY id"
    ledger_before = db.query(ledger)

    progress = _run(f)

    assert db.query(ledger) == ledger_before, "a converted copy is not a second call"
    assert progress["converted"] == 2
    assert [(r["id"], r["method"]) for r in _fetches()] == [
        (usable, "verification"),
        (newest, "verification"),
        (fetched, "ats text"),
    ]
    assert _answers(urls) == before
    rows = db.query("SELECT id, on_verdict FROM page_texts WHERE url = 'https://x/c' ORDER BY id")
    assert rows == [
        {"id": old, "on_verdict": True},
        {"id": usable, "on_verdict": False},
        {"id": newest, "on_verdict": False},
    ], "a converted copy is read once, as a fetch"
    assert db.query_one("SELECT 1 FROM ai_queries WHERE id = %s", (newest,)), "the answer stays"


def test_the_scheduler_queues_one_move_at_a_time(f):
    worker.schedule_ingest_cycle()
    db.execute(
        "UPDATE tasks SET dedupe_key = 'page-fetch-move:an-earlier-cycle' "
        "WHERE kind = 'move_page_fetches'"
    )
    worker.schedule_ingest_cycle()
    assert db.query("SELECT status FROM tasks WHERE kind = 'move_page_fetches'") == [
        {"status": "pending"}
    ]
