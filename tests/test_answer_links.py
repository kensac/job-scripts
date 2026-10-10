"""Pointing older answers at the page fetch they judged and the call that paid."""

from __future__ import annotations

import asyncio
from typing import Any

from api import db
from core.answers import VERIFY_INPUT_CHARS
from core.filters import build_custom_input
from tasks import answer_links
from tests.factories import legacy_answer

PAGE = "a posting body that is long enough to be a page " * 10


def _answer(url: str, check_type: str, copy: str, **columns: Any) -> int:
    """An answer as older writers stored it: a copy and no pointers."""
    return legacy_answer(url, "passed", "", check_type, input_content=copy, **columns)


def _run(f) -> dict:
    task_id = f.make_task("link_answers", status="running")
    asyncio.run(answer_links.handle_link_answers(task_id, {}))
    return db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]


def _pointer(answer_id: int) -> dict:
    return db.query_one(
        "SELECT page_fetch_id, model_call_id FROM ai_queries WHERE id = %s", (answer_id,)
    )


def _call(**columns: Any) -> int:
    row = {
        "purpose": "filter",
        "model": "gpt-5-nano",
        "batched": False,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "cached_tokens": 0,
        "source": "call",
        **columns,
    }
    names = ", ".join(row)
    values = ", ".join(f"%({k})s" for k in row)
    return db.query_one(f"INSERT INTO model_calls ({names}) VALUES ({values}) RETURNING id", row)[
        "id"
    ]


def test_a_copy_points_at_the_fetch_it_rebuilds_from(f):
    url = "https://links.test/custom"
    old = f.make_fetch(url, content="an older page " * 20)
    judged = f.make_fetch(url, content=PAGE)
    wrapped = _answer(
        url,
        "custom",
        build_custom_input("Acme", "Engineer", PAGE),
        company="Acme",
        job_title="Engineer",
        config_name="filter-batch",
    )
    later = f.make_fetch(url, content=PAGE)  # the same text fetched again, after
    # Verification asking a board's question read the page cut short.
    long_page = "x" * (VERIFY_INPUT_CHARS + 50)
    cut_fetch = f.make_fetch("https://links.test/cut", content=long_page)
    cut = _answer(
        "https://links.test/cut",
        "custom",
        build_custom_input("Acme", "Engineer", long_page[:VERIFY_INPUT_CHARS]),
        company="Acme",
        job_title="Engineer",
        config_name="verify-batch",
    )
    closed = _answer(url, "closed", PAGE, config_name="reverify")

    progress = _run(f)

    assert _pointer(wrapped)["page_fetch_id"] == judged, "the nearest at or before"
    assert _pointer(cut)["page_fetch_id"] == cut_fetch
    assert _pointer(closed)["page_fetch_id"] == later
    assert old not in {judged, later}
    assert progress["linked"]["fetches_stored"] == 0, "every text was already a fetch"
    assert progress["linked"]["pages_linked"] == 3
    assert _run(f)["total"] == 0, "a second run changes nothing"


def test_text_no_fetch_holds_becomes_a_fetch_before_its_copy_points_at_it(f):
    url = "https://links.test/unheld"
    seen = "the page as it was when it was judged " * 20
    closed = _answer(url, "closed", seen, config_name="reverify")
    clearance = _answer(url, "clearance", seen, config_name="reverify")
    custom = _answer(
        url,
        "custom",
        build_custom_input("Acme", "Engineer", "an older custom page " * 20),
        company="Acme",
        job_title="Engineer",
        config_name="filter-batch",
    )
    f.make_fetch(url, content="the page as it is now " * 20)
    # Newer than every fetch of its url: storing its text would make it the
    # url's current page, so it keeps its copy.
    newest = _answer("https://links.test/newest", "closed", seen, config_name="reverify")
    f.make_fetch("https://links.test/newest", content="an earlier page " * 20)
    db.execute("UPDATE page_fetches SET id = id - 1000 WHERE url = 'https://links.test/newest'")

    progress = _run(f)

    assert _pointer(newest)["page_fetch_id"] is None
    stored = db.query(
        "SELECT id, method, content FROM page_fetches WHERE method = 'verification' ORDER BY id"
    )
    assert [(s["id"], s["content"]) for s in stored] == [
        (closed, seen),
        (custom, "an older custom page " * 20),
    ], "one fetch per text, under the first answer that saw it, without its header"
    assert _pointer(closed)["page_fetch_id"] == _pointer(clearance)["page_fetch_id"] == closed
    assert _pointer(custom)["page_fetch_id"] == custom
    assert progress["linked"]["fetches_stored"] == 2
    newest = db.query_one(
        "SELECT input_content FROM page_texts WHERE url = %s ORDER BY id DESC LIMIT 1", (url,)
    )
    assert newest["input_content"] == "the page as it is now " * 20, (
        "an older answer's text does not become the url's current page"
    )


def test_a_copy_whose_header_its_columns_do_not_rebuild_is_left_alone(f):
    url = "https://links.test/header"
    f.make_fetch(url, content=PAGE)
    stray = _answer(
        url,
        "custom",
        build_custom_input("Someone Else", "Engineer", PAGE),
        company="Acme",
        job_title="Engineer",
        config_name="filter-batch",
    )

    _run(f)

    assert _pointer(stray)["page_fetch_id"] is None
    assert not db.query("SELECT 1 FROM page_fetches WHERE method = 'verification'")


def test_each_answer_finds_its_call(f):
    url = "https://links.test/calls"
    batched = legacy_answer(url, "passed", "", "closed", batch_id="b-1", total_tokens=110)
    sibling = legacy_answer(url, "passed", "", "clearance", batch_id="b-1", total_tokens=0)
    item = _call(provider_batch_id="b-1", custom_id=url, batched=True)
    copied = legacy_answer(
        url, "passed", "", "custom", model="gpt-5-nano", total_tokens=110, duration_ms=900
    )
    copied_call = _call(source="verdict", source_id=copied)
    booked = legacy_answer(
        url,
        "passed",
        "",
        "custom",
        model="gpt-5-nano",
        prompt_tokens=7,
        completion_tokens=3,
        total_tokens=10,
    )
    booked_call = _call(source="usage", prompt_tokens=7, completion_tokens=3, total_tokens=10)
    # Two answers and two calls with the same numbers in the same minute:
    # nothing says which paid which, so neither is linked.
    twins = [
        legacy_answer(
            url,
            "passed",
            "",
            "custom",
            model="gpt-5-nano",
            prompt_tokens=5,
            completion_tokens=5,
            total_tokens=10,
        )
        for _ in range(2)
    ]
    for _ in range(2):
        _call(source="call", prompt_tokens=5, completion_tokens=5, total_tokens=10)
    # A live caller booked its call after run_check wrote the verdict.
    db.execute("UPDATE model_calls SET created_at = now() + interval '1 second'")
    db.execute(
        "UPDATE model_calls SET created_at = now() - interval '1 hour' WHERE source = 'verdict'"
    )

    _run(f)

    assert _pointer(batched)["model_call_id"] == _pointer(sibling)["model_call_id"] == item
    assert _pointer(copied)["model_call_id"] == copied_call
    assert _pointer(booked)["model_call_id"] == booked_call
    assert [_pointer(t)["model_call_id"] for t in twins] == [None, None]
    durations = db.query_one("SELECT duration_ms FROM model_calls WHERE id = %s", (copied_call,))
    assert durations["duration_ms"] == 900, "the verdict's wall time moves to its call"


def test_the_worker_stops_queueing_once_a_run_linked_nothing(f):
    from api import worker

    def queued() -> int:
        return db.query_one(
            "SELECT count(*) AS n FROM tasks WHERE kind = 'link_answers' AND status = 'pending'"
        )["n"]

    worker.schedule_ingest_cycle()
    assert queued() == 1
    db.execute(
        "UPDATE tasks SET status = 'done', progress = '{\"total\": 0}'::jsonb "
        "WHERE kind = 'link_answers'"
    )
    worker.schedule_ingest_cycle()
    assert queued() == 0
