"""Twins: one company's posting text listed at several places, read once."""

from __future__ import annotations

import pytest

from api import db
from core.near_copy import key
from tasks import verify as tasks_verify
from tests.test_verify_board_questions import _board

BODY = "We are hiring a store associate.\nYou will stock shelves and help customers.\n" * 5


def test_places_and_numbers_do_not_make_a_new_posting():
    a = key("Store Associate", "Store #1234\nAustin, TX\n" + BODY + "Pay: $15.50/hr")
    b = key("Store Associate", "Store #98\nDenver, CO\n" + BODY + "Pay: $17.00/hr")
    assert a == b


def test_the_title_and_the_job_text_do():
    assert key("Store Associate", BODY) != key("Software Engineer", BODY)
    assert key("Store Associate", BODY) != key("Store Associate", BODY + "Requires a CDL.")


@pytest.fixture
def submitted(monkeypatch):
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('verification_reachability_gate_enabled', "
        "'true') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    specs: list = []

    async def fake_run_batched(task_id, task, batch):
        specs.extend(batch)
        return [], None

    monkeypatch.setattr(tasks_verify, "run_batched", fake_run_batched)
    return specs


async def _sweep(f) -> None:
    await tasks_verify.handle_verify_new(f.make_task("verify_new", {}, status="running"), {})


def _content(url: str, text: str) -> None:
    db.execute(
        "UPDATE page_fetches SET content = %s WHERE url = %s",
        (text, url),
    )


@pytest.mark.asyncio
async def test_a_twin_of_a_verified_posting_takes_its_verdicts_and_is_not_asked(f, submitted):
    source = f.make_source("near-copy-src")
    board = _board(f, source, slug="near-copy-board")
    twin_id, twin = f.make_ready_job(source=source, title="Store Associate")
    _content(twin, "Austin, TX\n" + BODY)
    db.execute("UPDATE ai_queries SET reason = 'no closure' WHERE url = %s", (twin,))
    f.make_verdict(twin, "custom", "rejected", prompt_hash=board["prompt_hash"])
    db.execute(
        "UPDATE ai_queries SET model = 'gpt-6-luna' WHERE url = %s AND check_type = 'custom'",
        (twin,),
    )
    # Keyed when it was verified, as verify_new keys every candidate it reads.
    db.execute(
        "UPDATE jobs SET near_copy_key = %s WHERE id = %s",
        (key("Store Associate", "Austin, TX\n" + BODY), twin_id),
    )
    _, copy = f.make_ready_job(source=source, title="Store Associate", closed="", clearance="")
    _content(copy, "Denver, CO\n" + BODY)

    await _sweep(f)

    assert copy not in [spec.custom_id for spec in submitted]
    rows = db.query(
        "SELECT check_type, status, config_name, prompt_hash, model FROM ai_queries "
        "WHERE url = %s ORDER BY check_type",
        (copy,),
    )
    assert [(r["check_type"], r["status"], r["config_name"]) for r in rows] == [
        ("clearance", "passed", "verify-near-copy"),
        ("closed", "passed", "verify-near-copy"),
        ("custom", "rejected", "verify-near-copy"),
    ]
    assert rows[2]["prompt_hash"] == board["prompt_hash"] and rows[2]["model"] == "gpt-6-luna"


@pytest.mark.asyncio
async def test_twins_in_one_sweep_are_read_once(f, submitted):
    source = f.make_source("near-copy-src-2")
    _board(f, source, slug="near-copy-board-2")
    _, a = f.make_ready_job(source=source, title="Store Associate", closed="", clearance="")
    _, b = f.make_ready_job(source=source, title="Store Associate", closed="", clearance="")
    _content(a, "Austin, TX\n" + BODY)
    _content(b, "Denver, CO\n" + BODY)

    await _sweep(f)

    assert len([s for s in submitted if s.custom_id in (a, b)]) == 1


@pytest.mark.asyncio
async def test_switched_off_reads_every_twin(f, submitted):
    source = f.make_source("near-copy-src-3")
    _board(f, source, slug="near-copy-board-3")
    _, a = f.make_ready_job(source=source, title="Store Associate", closed="", clearance="")
    _, b = f.make_ready_job(source=source, title="Store Associate", closed="", clearance="")
    _content(a, "Austin, TX\n" + BODY)
    _content(b, "Denver, CO\n" + BODY)
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('verify_near_copy_reuse', 'false') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )

    await _sweep(f)

    assert {s.custom_id for s in submitted} >= {a, b}
