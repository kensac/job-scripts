"""A task on several batches collects the ones that finished and parks again
on the rest.

On 2026-09-04 fourteen filter chunks sat 14 hours parked on batches at 207 of
211 and 76 of 81 requests, holding ~2,900 already-paid verdicts uncollectable
beside them, and 0 of 6,226 new postings reached the board. Every batch is
either read whole or not at all, so the partial unit is the batch: the poll
resumes once some are terminal and the rest have run past
batch_straggler_hours, the handler takes what landed, the worker parks it on
what has not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import db, worker
from api.tasks import batches as tasks_batches
from api.tasks import runtime
from core.batch import BatchProgress
from tests.factories import make_task

COMPLETED_LINE = (
    '{"custom_id": "u1", "response": {"status_code": 200, "body": '
    '{"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}], '
    '"usage": {"total_tokens": 3}}}}'
)


class _FakeClient:
    def __init__(self, states: dict[str, str]):
        self.states = states
        self.batches = SimpleNamespace(retrieve=self._retrieve)
        self.files = SimpleNamespace(content=self._content)

    async def _retrieve(self, batch_id: str):
        state = self.states[batch_id]
        if state == "boom":
            raise RuntimeError("provider unreachable")
        return SimpleNamespace(
            id=batch_id,
            status=state,
            output_file_id="f1" if state == "completed" else None,
            error_file_id=None,
            request_counts=SimpleNamespace(total=1, completed=1, failed=0),
        )

    async def _content(self, file_id: str):
        return SimpleNamespace(text=COMPLETED_LINE)


def _ai_batch(batch_id: str, status: str, hours: float) -> None:
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, model, requests, completed, "
        "failed_count, status, submitted_at) VALUES (%s, 'filter', 'm', 1, 0, 0, %s, "
        "now() - make_interval(secs => %s))",
        (batch_id, status, int(hours * 3600)),
    )


def _status(task_id: int) -> str:
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row is not None
    return row["status"]


@pytest.mark.asyncio
async def test_collect_finished_batches_takes_terminal_and_reports_the_rest(monkeypatch):
    from core import batch

    monkeypatch.setattr(
        batch, "_client", lambda: _FakeClient({"a": "completed", "b": "in_progress", "c": "boom"})
    )
    results, unfinished = await batch.collect_finished_batches(["a", "b", "c"])
    assert results["u1"].text == "ok" and results["u1"].batch_id == "a"
    # Unreadable counts as unfinished: a provider blip delays, never drops.
    assert unfinished == ["b", "c"]


@pytest.mark.asyncio
async def test_collect_pending_rewrites_payload_to_what_is_still_running(monkeypatch):
    tid = make_task("run_filter", {"batch_ids": ["a", "b"]}, status="running")

    async def fake(ids, on_event=None):
        return {"u1": object()}, ["b"]

    monkeypatch.setattr("core.batch.collect_finished_batches", fake)
    assert list(await runtime.collect_pending(tid, None)) == ["u1"]
    assert runtime._pending_batch_ids(tid) == ["b"]

    async def all_done(ids, on_event=None):
        return {}, []

    monkeypatch.setattr("core.batch.collect_finished_batches", all_done)
    await runtime.collect_pending(tid, None)
    assert runtime._pending_batch_ids(tid) == []


def test_repark_only_when_ids_remain():
    assert runtime.repark_if_unfinished(make_task("run_filter", {}, status="running")) is False
    tid = make_task("run_filter", {"batch_ids": ["b"]}, status="running")
    assert runtime.repark_if_unfinished(tid) is True
    assert _status(tid) == "awaiting_batch"


@pytest.mark.asyncio
async def test_worker_parks_a_handler_that_returns_with_batches_left(monkeypatch):
    async def partial(task_id, payload):
        runtime._set_batch_ids(task_id, ["b"])

    monkeypatch.setitem(worker.HANDLERS, "test_kind", partial)
    tid = runtime.enqueue("test_kind", {})
    assert await worker.run_once() is True
    assert _status(tid) == "awaiting_batch"
    assert runtime._pending_batch_ids(tid) == ["b"]


@pytest.mark.asyncio
async def test_poll_resumes_on_stragglers_only_past_the_configured_hours(monkeypatch):
    async def progress(ids):
        return {
            "a": BatchProgress(status="completed", total=1, completed=1),
            "b": BatchProgress(status="in_progress", total=1),
        }

    monkeypatch.setattr("core.batch.batch_progress", progress)
    poll = make_task("poll_batches", {}, status="running")

    # The straggler is younger than the threshold (default 4h): keep waiting.
    young = make_task("run_filter", {"batch_ids": ["a", "b"]}, status="awaiting_batch")
    _ai_batch("a", "completed", 5)
    _ai_batch("b", "in_progress", 1)
    await tasks_batches.handle_poll_batches(poll, {})
    assert _status(young) == "awaiting_batch"

    # Older than the threshold beside a finished sibling: resume to collect.
    db.execute(
        "UPDATE ai_batches SET submitted_at = now() - interval '5 hours' WHERE provider_batch_id = 'b'"
    )
    await tasks_batches.handle_poll_batches(poll, {})
    assert _status(young) == "pending"

    # Nothing finished at all: age alone resumes nothing before the window.
    monkeypatch.setattr(
        "core.batch.batch_progress",
        lambda ids: progress_none(),
    )
    none_done = make_task("run_filter", {"batch_ids": ["b"]}, status="awaiting_batch")
    await tasks_batches.handle_poll_batches(poll, {})
    assert _status(none_done) == "awaiting_batch"


async def progress_none():
    return {"b": BatchProgress(status="in_progress", total=1)}


def test_straggler_hours_is_admin_config(client, admin_headers):
    r = client.put(
        "/v1/admin/config/batch_straggler_hours", json={"value": 6}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert db.get_config("batch_straggler_hours") == 6
