"""The verdict-cache check is one query per run, with the per-job answers.

Task 5834132 (run_managed_board_batch, 23,394 candidates) spent hours on the
`oci` worker asking this once per candidate, about three round trips each at
~103 ms (pg_stat_activity, 2026-10-04). The same loop costs a minute or two on
a host beside the database, which is why it was invisible until a far worker
took the task.
"""

from __future__ import annotations

import psycopg
import pytest

from api import ai, db
from api.model_calls import Payer
from core import query_instructions, store
from core.filters import compute_filter_hash
from core.pool import connection
from core.query_instructions import InstructionUnavailable
from core.store import add_ai_result
from tasks import filter_execution
from tasks import managed_boards as managed_task
from tests.factories import board_config


def _per_job(url: str, prompt_hash: str, model: str | None = None) -> bool:
    """The per-candidate check as it stood before batching, kept as the oracle."""
    clause = " AND model = %s" if model is not None else ""
    params = (url, prompt_hash, model) if model is not None else (url, prompt_hash)
    with connection() as conn:
        row = conn.execute(
            "SELECT instructions_id "
            "FROM ai_queries WHERE url = %s AND check_type = 'custom' "
            f"AND prompt_hash = %s{clause} AND status IN ('passed', 'rejected') "
            "ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
    if row is None:
        return False
    query_instructions.hydrate([row])
    return True


def _corrupt(query_id: int) -> None:
    db.execute(
        "UPDATE ai_instruction_texts SET instructions='corrupted' "
        "WHERE id=(SELECT instructions_id FROM ai_queries WHERE id=%s)",
        (query_id,),
    )


def _custom(url: str, status: str = "passed", **kwargs) -> int:
    kwargs.setdefault("prompt_hash", "h1")
    kwargs.setdefault("model", "m1")
    kwargs.setdefault("instructions", f"instructions for {url}")
    return add_ai_result(url, status, check_type="custom", **kwargs)


def _mixed_fixture() -> list[str]:
    u = [f"https://cache.test/{name}" for name in range(10)]
    _custom(u[0])  # referenced instructions
    _custom(u[1], "rejected")  # rejected is decided too
    # u[2]: nothing at all
    _custom(u[3], "failed")  # undecided only
    _custom(u[4], prompt_hash="h2")  # another filter
    _custom(u[5], model="m2")  # another model
    _custom(u[6])  # decided, then a later failure: still decided
    _custom(u[6], "failed")
    add_ai_result(u[7], "passed", check_type="closed", prompt_hash="h1", model="m1")
    # The latest decided row is the one hydrated; an older broken one is not read.
    _corrupt(_custom(u[8], instructions="older"))
    _custom(u[8], instructions="newer")
    _custom(u[9], instructions=None)  # decided with no instructions recorded
    return u


@pytest.mark.parametrize(
    ("prompt_hash", "model"),
    [("h1", "m1"), ("h1", "m2"), ("h1", None), ("h2", "m1"), ("h2", None), ("h3", None)],
)
def test_batched_answers_match_the_per_job_check(prompt_hash, model):
    urls = _mixed_fixture()
    expected = {url for url in urls if _per_job(url, prompt_hash, model)}
    assert store.decided_custom_urls(urls, prompt_hash, model) == expected


def test_the_fixture_distinguishes_every_condition():
    """Guards the parametrised comparison from passing on an all-miss fixture."""
    u = _mixed_fixture()
    assert store.decided_custom_urls(u, "h1", "m1") == {u[0], u[1], u[6], u[8], u[9]}
    assert store.decided_custom_urls(u, "h1", None) == {u[0], u[1], u[5], u[6], u[8], u[9]}
    assert store.decided_custom_urls(u, "h2", None) == {u[4]}
    assert store.decided_custom_urls([], "h1", "m1") == set()


def test_a_corrupt_referenced_dictionary_entry_raises():
    url = "https://cache.test/corrupt"
    _corrupt(_custom(url))
    with pytest.raises(InstructionUnavailable):
        _per_job(url, "h1", "m1")
    with pytest.raises(InstructionUnavailable):
        store.decided_custom_urls(["https://cache.test/fine", url], "h1", "m1")


def test_a_missing_referenced_dictionary_entry_raises():
    url = "https://cache.test/missing"
    query_id = _custom(url)
    with db.transaction():
        db.execute("SET LOCAL session_replication_role = replica")
        db.execute(
            "DELETE FROM ai_instruction_texts "
            "WHERE id=(SELECT instructions_id FROM ai_queries WHERE id=%s)",
            (query_id,),
        )
    with pytest.raises(InstructionUnavailable):
        _per_job(url, "h1", "m1")
    with pytest.raises(InstructionUnavailable):
        store.decided_custom_urls([url], "h1", "m1")


def _record_statements(monkeypatch) -> list[str]:
    statements: list[str] = []
    execute = psycopg.Cursor.execute

    def recording(cursor, query, *args, **kwargs):
        statements.append(str(query))
        return execute(cursor, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", recording)
    return statements


def _cache_reads(statements: list[str]) -> list[str]:
    return [s for s in statements if "check_type = 'custom'" in s and "prompt_hash" in s]


N = 40


@pytest.mark.asyncio
async def test_managed_batch_admission_reads_the_cache_once_for_n_candidates(f, monkeypatch):
    monkeypatch.setattr("core.routing.server_key", lambda provider: "test-server-key")
    sponsor = f.make_user(groups=["infra-admins"])
    source = f.make_source("managed-cache-source")
    prompt_hash = compute_filter_hash("prompt", "filter")
    jobs = []
    for index in range(N):
        job_id, url = f.make_ready_job(source=source)
        jobs.append(
            {
                "id": job_id,
                "url": url,
                "company": "Acme",
                "title": "Engineer",
                "source": source,
                "title_gate_keep": True,
                "sort_at": "2026-09-01T00:00:00+00:00",
            }
        )
        if index % 2:
            _custom(url, prompt_hash=prompt_hash, model="gpt-5.6-luna")
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model, on_ambiguous, fail_closed, criteria) "
        "VALUES ('managed-cache', 'Managed cache', %s, 'prompt', %s, "
        "'gpt-5.6-luna', 'filter', true, '{}') RETURNING id, revision",
        (sponsor, prompt_hash),
    )
    assert board is not None
    payload = {
        "managed_board_id": board["id"],
        "sponsor_user_id": sponsor,
        "revision": board["revision"],
        "prompt": "prompt",
        "prompt_hash": prompt_hash,
        "requested_model": "gpt-5.6-luna",
        "execution_mode": "managed_filter",
        "execution_version": 2,
        "inference_transport": "batch",
        "reasoning_effort": "medium",
        "on_ambiguous": "filter",
        "fail_closed": True,
        "sources": [source],
        "criteria": {},
        "published": False,
        "reserved_tokens": 1000,
        "jobs": jobs,
    }
    payload = board_config(payload)
    task_id = f.make_task("run_managed_board_batch", payload, status="running")
    submitted = []

    async def fake_batch(task_id, cfg, snapshot, inference_jobs, hooks, **kwargs):
        submitted.extend(job["url"] for job in inference_jobs)

    monkeypatch.setattr(managed_task, "execute_batch", fake_batch)
    statements = _record_statements(monkeypatch)
    await managed_task.handle_run_managed_board_batch(task_id, payload)

    assert submitted == [job["url"] for index, job in enumerate(jobs) if not index % 2]
    assert len(_cache_reads(statements)) == 1


@pytest.mark.asyncio
async def test_live_execution_reads_cache_and_content_once_for_n_candidates(f, monkeypatch):
    jobs = []
    for index in range(N):
        _, url = f.make_ready_job()
        jobs.append({"url": url, "company": "Acme", "title": "Engineer"})
        if index % 2:
            _custom(url, prompt_hash="live-hash", model="gpt-5.6-luna")
    checked = []

    async def run_check(cfg, *, url, input_text, **kwargs):
        checked.append(url)
        return None, {"total_tokens": 1}

    monkeypatch.setattr(filter_execution.verdicts, "run_check", run_check)
    hooks = filter_execution.ExecutionHooks(
        verdict_label="live-cache",
        key_source="owner",
        payer=Payer(user_id=1),
        purpose="filter",
        record_usage=lambda usage, model, batched: None,
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda done, total, label: None,
        complete=lambda: None,
    )
    statements = _record_statements(monkeypatch)
    await filter_execution.execute_live(
        f.make_task("run_filter_chunk"),
        ai.AIConfig("openai", "key", "owner", "gpt-5.6-luna"),
        filter_execution.FilterSnapshot("live", "prompt", "filter", "live-hash"),
        jobs,
        hooks,
    )

    assert sorted(checked) == sorted(job["url"] for i, job in enumerate(jobs) if not i % 2)
    assert len(_cache_reads(statements)) == 1
    content_reads = [s for s in statements if "FROM page_texts" in s]
    assert len(content_reads) == 1
