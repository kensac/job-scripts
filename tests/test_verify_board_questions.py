"""Boards asked inside the new-posting verification request.

Every board reviews the posting text verification has just read, so the sweep
asks their questions in the same request and the boards' runs reuse the
verdicts instead of paying for the text again.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import db
from core.answers import joint_question_key
from core.filters import compute_filter_hash
from core.store import decided_custom_urls
from tasks import verify
from tests.factories import make_batch_result

PROMPT = "Entry-level software engineering roles."


def _board(f, source: str, *, slug: str = "joint-board", title_gate: dict | None = None) -> dict:
    sponsor = f.make_user()
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model, title_gate, published, published_at, public_revision) "
        "VALUES (%s, %s, %s, %s, %s, 'gpt-6-luna', %s, true, now(), 1) "
        "RETURNING id, prompt_hash",
        (
            slug,
            slug,
            sponsor,
            PROMPT,
            compute_filter_hash(PROMPT, "keep"),
            db.jsonb(title_gate) if title_gate else None,
        ),
    )
    db.execute(
        "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
        (board["id"], source),
    )
    return board


@pytest.fixture
def submitted(monkeypatch):
    # Production reaches postings through boards; the legacy branch reads subscriptions only.
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('verification_reachability_gate_enabled', "
        "'true') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    specs: list = []

    async def fake_run_batched(task_id, task, batch):
        specs.extend(batch)
        return [], None

    monkeypatch.setattr(verify, "run_batched", fake_run_batched)
    return specs


async def _sweep(f) -> None:
    task_id = f.make_task("verify_new", {}, status="running")
    await verify.handle_verify_new(task_id, {})


@pytest.mark.asyncio
async def test_a_board_that_admits_the_posting_is_asked_in_the_same_request(f, submitted):
    source = f.make_source("joint-src")
    board = _board(f, source)
    _, url = f.make_ready_job(source=source, closed="", clearance="")

    await _sweep(f)

    [spec] = submitted
    assert spec.custom_id == url
    assert joint_question_key(board["id"]) in spec.schema["properties"]
    assert [b["prompt_hash"] for b in spec.context["boards"]] == [board["prompt_hash"]]
    assert PROMPT in spec.instructions
    assert spec.input.startswith("Company: ")


@pytest.mark.asyncio
async def test_a_posting_no_board_admits_keeps_the_plain_request(f, submitted):
    source = f.make_source("joint-src-plain")
    _board(f, f.make_source("another-src"))
    job_id, url = f.make_ready_job(source=source, closed="", clearance="")
    f.make_board_row(f.make_user(), job_id)

    await _sweep(f)

    [spec] = submitted
    content = db.query_one(
        "SELECT input_content FROM ai_queries WHERE url = %s AND check_type = 'content'", (url,)
    )["input_content"]
    plain = verify._verification_spec(url, content)
    assert (spec.instructions, spec.input, spec.schema) == (
        plain.instructions,
        plain.input,
        plain.schema,
    )
    assert "boards" not in spec.context


@pytest.mark.asyncio
async def test_switched_off_asks_no_board(f, submitted):
    source = f.make_source("joint-src-off")
    _board(f, source)
    f.make_ready_job(source=source, closed="", clearance="")
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('verify_answers_board_questions', 'false') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )

    await _sweep(f)

    assert "boards" not in submitted[0].context


@pytest.mark.asyncio
async def test_enforced_title_gate_and_decided_postings_are_not_asked(f, submitted):
    source = f.make_source("joint-src-gated")
    gated = _board(f, source, title_gate={"recipe": "internship_v1", "mode": "enforce"})
    f.make_ready_job(source=source, closed="", clearance="", title="Senior Staff Engineer")
    _, decided_url = f.make_ready_job(
        source=source, closed="", clearance="", title="Software Engineering Intern"
    )
    f.make_verdict(decided_url, "custom", "passed", prompt_hash=gated["prompt_hash"])
    db.execute(
        "UPDATE ai_queries SET model = 'gpt-6-luna' WHERE url = %s AND check_type = 'custom'",
        (decided_url,),
    )

    await _sweep(f)

    assert all("boards" not in spec.context for spec in submitted)


async def _collect(f, monkeypatch, spec, *, model: str):
    answer = {
        "verification": {
            "is_closed": False,
            "closed_reason": "open",
            "requires_clearance_or_restrictions": False,
            "clearance_reason": "none",
        },
        **{
            joint_question_key(b["board_id"]): {"should_filter": False}
            for b in spec.context["boards"]
        },
    }
    task = f.make_task("verify_new", {"batch_ids": ["joint-batch"]})
    result = make_batch_result(
        task,
        spec,
        text=json.dumps(answer),
        model=model,
        usage={"input_tokens": 2000, "output_tokens": 100, "total_tokens": 2100},
        batch_id="joint-batch",
    )

    async def collect(*args):
        return [result], SimpleNamespace(model=model)

    monkeypatch.setattr(verify, "run_batched", collect)
    await verify.handle_verify_new(task, {})


@pytest.mark.asyncio
async def test_collection_writes_the_board_verdict_the_board_run_then_reuses(
    f, submitted, monkeypatch
):
    source = f.make_source("joint-src-collect")
    board = _board(f, source)
    _, url = f.make_ready_job(source=source, closed="", clearance="")
    await _sweep(f)
    [spec] = submitted

    await _collect(f, monkeypatch, spec, model=spec.context["model"])

    rows = db.query(
        "SELECT check_type, status, prompt_hash, filter_name, total_tokens, request_sha256 "
        "FROM ai_queries WHERE url = %s AND check_type <> 'content' ORDER BY id",
        (url,),
    )
    assert [(r["check_type"], r["status"]) for r in rows] == [
        ("closed", "passed"),
        ("clearance", "passed"),
        ("custom", "passed"),
    ]
    assert rows[2]["prompt_hash"] == board["prompt_hash"]
    assert rows[2]["filter_name"] == f"managed-board:{board['id']}"
    assert [r["total_tokens"] for r in rows] == [2100, 0, 0], "one call, booked once"
    assert rows[0]["request_sha256"] == spec.context["verify_question"]
    assert decided_custom_urls([url], board["prompt_hash"], model=spec.context["model"]) == {url}


@pytest.mark.asyncio
async def test_a_different_answering_model_writes_no_board_verdict(f, submitted, monkeypatch):
    source = f.make_source("joint-src-model")
    _board(f, source)
    _, url = f.make_ready_job(source=source, closed="", clearance="")
    await _sweep(f)

    await _collect(f, monkeypatch, submitted[0], model="some-other-model")

    checks = {
        r["check_type"]
        for r in db.query("SELECT check_type FROM ai_queries WHERE url = %s", (url,))
    }
    assert checks == {"content", "closed", "clearance"}


def test_the_joint_request_asks_for_reasons_only_on_a_flagged_axis():
    from core.answers import _VERIFY_INSTRUCTIONS, joint_verification

    instructions, _ = joint_verification({1: "criteria"})
    assert "When that axis is false, an empty string." in instructions
    assert "When that axis is false" not in _VERIFY_INSTRUCTIONS, "the plain request is unchanged"
