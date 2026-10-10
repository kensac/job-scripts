"""Batch result collection is one transaction per chunk, not one per result.

Per result, collection cost about 9 round trips (BEGIN, SAVEPOINT, five
statements, RELEASE, COMMIT). A 23,394-result managed batch on the `oci`
worker, about 103 ms from the database, is then roughly 6 h of round trips
(measured in #769's investigation). The chunked form must leave the database
exactly as the per-result form does, keep each receipt exactly-once across a
crash, and isolate a poisoned result the way the per-result form does.

The per-result form is still in the code as the fallback for a chunk that
fails, and these tests use it as the oracle by making every chunk fail.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import socket
import threading
import time
from dataclasses import replace

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import core.pool
from api import budget, db
from api.ai import batch_results
from api.model_calls import Payer
from core.answers import FilterDecision
from core.batch import BatchResult, structured_response_spec
from tasks import filter_execution
from tests.conftest import _reset_to_empty

SNAPSHOT = filter_execution.FilterSnapshot("chunked", "prompt", "filter", "chunk-hash")
MODEL = "gpt-5-nano"


def _usage(i: int) -> dict:
    return {
        "input_tokens": 100 + i,
        "output_tokens": 10 + i,
        "total_tokens": 110 + 2 * i,
        "input_tokens_details": {"cached_tokens": i % 3},
        "output_tokens_details": {"reasoning_tokens": i % 4},
    }


# One of each thing collection distinguishes, by index.
_SHAPES = [
    {"text": '{"should_filter":false}'},
    {"text": '{"should_filter":true,"reason":"excluded"}'},
    {"error": "server_error"},
    {"text": "not json"},
    {},
    {"text": '{"should_filter":false}', "usage": None},
]


class Ledger:
    """Hooks that write the real usage ledger and remember every call."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.calls: list[tuple] = []

    def hooks(self) -> filter_execution.ExecutionHooks:
        def record_usage(usage, model, batched):
            self.calls.append((json.dumps(usage, sort_keys=True), model, batched))
            budget.record_tokens(self.user_id, "owner", "filter", model, usage, batched=batched)

        return filter_execution.ExecutionHooks(
            verdict_label="chunk-filter",
            key_source="owner",
            payer=Payer(user_id=1),
            purpose="filter",
            record_usage=record_usage,
            budget_exceeded=lambda: False,
            cancelled=lambda: False,
            progress=lambda *_: None,
            complete=lambda: None,
        )


def _scenario(f, n: int, poison: str | None = None) -> tuple[int, Ledger, list[BatchResult]]:
    """A checkpointed batch of n results plus the awkward cases, in collection order."""
    user = f.make_user(sub="chunk-user", email="chunk@example.test")
    task = f.make_task("run_filter_batch_chunk", status="running")
    jobs = [
        {"url": f"https://chunk.test/{i:04d}", "title": f"Role {i}", "company": "Example"}
        for i in range(n)
    ]
    specs, results = [], []
    for i, job in enumerate(jobs):
        context = {
            "job": job,
            "filter": SNAPSHOT.__dict__,
            "reasoning_effort": "low",
        }
        if poison == "python" and i == n // 2:
            context["job"] = {"url": job["url"], "title": job["title"]}  # no company
        if poison == "database" and i == n // 2:
            # PostgreSQL text cannot hold NUL, so the verdict insert fails.
            context["job"] = {**job, "company": "Exam\x00ple"}
        specs.append(
            structured_response_spec(
                job["url"],
                "instructions " + str(i % 2),
                f"input {i}",
                FilterDecision,
                context=context,
            )
        )
        shape = _SHAPES[i % len(_SHAPES)]
        results.append(
            BatchResult(
                job["url"],
                text=shape.get("text"),
                error=shape.get("error"),
                usage=shape.get("usage", _usage(i)),
                model=MODEL,
                batch_id=f"batch-{i % 2}",
            )
        )
    # A paid result whose request was never recorded.
    results.append(
        BatchResult("https://chunk.test/unknown", usage=_usage(99), model=MODEL, batch_id="batch-0")
    )
    for batch_id in ("batch-0", "batch-1"):
        db.execute(
            "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model, payer, payer_id) "
            "VALUES (%s, %s, 'filter', %s, 'user', %s)",
            (batch_id, task, MODEL, user),
        )
    batch_results.snapshot_specs(task, specs)
    batch_results.checkpoint(task, results, [])
    collected = batch_results.unconsumed(task)
    # A receipt already consumed before this run, collected again.
    first = collected[0]
    with batch_results.consume_result(task, first) as receipt:
        receipt.outcome = "written"
    collected = [*collected[1:], first]
    # The same result twice in one collection.
    collected.insert(2, collected[1])
    if poison == "unreceipted":
        ghost = BatchResult("https://chunk.test/ghost", model=MODEL, batch_id="batch-9")
        collected.insert(len(collected) // 2, ghost)
    return task, Ledger(user), collected


async def _collect(task: int, ledger: Ledger, results: list[BatchResult]) -> None:
    async def collect(*_args):
        return list(results)

    await filter_execution.execute_batch(
        task, None, SNAPSHOT, [], ledger.hooks(), contents={}, unavailable=0, collect=collect
    )


def _per_result(monkeypatch) -> None:
    """Make every chunk fail so the per-result fallback does all the work."""

    def refuse(*_args, **_kwargs):
        raise RuntimeError("chunk refused")

    monkeypatch.setattr(filter_execution, "_collect_chunk", refuse)


_TIMESTAMPS = (
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema='public' AND data_type LIKE 'timestamp%%'"
)


def _state() -> dict[str, list[str]]:
    """Every row of every table, minus the columns that hold a wall clock."""
    clocks: dict[str, list[str]] = {}
    for row in db.query(_TIMESTAMPS):
        clocks.setdefault(row["table_name"], []).append(row["column_name"])
    tables = [
        row["tablename"]
        for row in db.query(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename <> 'alembic_version' ORDER BY tablename"
        )
    ]
    state = {}
    for table in tables:
        rows = db.query(
            f'SELECT (to_jsonb(t) - %s::text[])::text AS r FROM "{table}" t',
            (clocks.get(table, []),),
        )
        state[table] = sorted(row["r"] for row in rows)
    return state


async def _run(
    f, monkeypatch, n: int, *, per_result: bool, poison: str | None = None
) -> tuple[dict, list, type | None, int]:
    _reset_to_empty()
    with monkeypatch.context() as patch:
        if per_result:
            _per_result(patch)
        task, ledger, results = _scenario(f, n, poison)
        error = None
        try:
            await _collect(task, ledger, results)
        except Exception as exc:
            error = type(exc)
    return _state(), ledger.calls, error, task


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk", [None, 4])
async def test_chunked_collection_leaves_the_per_result_state(f, monkeypatch, chunk):
    if chunk:
        monkeypatch.setattr(filter_execution, "COLLECT_CHUNK", chunk)
    expected, expected_calls, error, task = await _run(f, monkeypatch, 13, per_result=True)
    assert error is None
    # Guard against a vacuous comparison: the fixture writes every kind of row.
    outcomes = batch_results.outcome_counts(task)
    assert outcomes == {"written": 7, "failed": 6, "unknown_request": 1}
    assert len(expected["ai_queries"]) == 12  # one result was consumed before the run
    # Booked with the receipts, before collection: every paid result, the
    # stranger included, since it was billed; the two without usage were not.
    assert len(expected["model_calls"]) == 12
    actual, calls, error, _ = await _run(f, monkeypatch, 13, per_result=False)
    assert error is None
    assert calls == expected_calls
    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("poison", ["python", "database", "unreceipted"])
async def test_a_poisoned_result_is_isolated_as_per_result_collection_isolates_it(
    f, monkeypatch, poison
):
    _, expected_calls, expected_error, task = await _run(
        f, monkeypatch, 12, per_result=True, poison=poison
    )
    assert expected_error is not None
    # What per-result collection does: everything before the poison commits,
    # the poison and everything after it stay unconsumed for the retry.
    consumed = db.query_one(
        "SELECT count(*) FILTER (WHERE consumed_at IS NOT NULL) AS done, "
        "count(*) FILTER (WHERE consumed_at IS NULL) AS waiting "
        "FROM batch_result_receipts WHERE task_id=%s",
        (task,),
    )
    assert consumed["done"] > 1 and consumed["waiting"] > 1
    expected_rows = _rows(task)
    _, calls, error, task = await _run(f, monkeypatch, 12, per_result=False, poison=poison)
    assert error is expected_error
    assert calls == expected_calls
    # The failed chunk drew ids from sequences before it rolled back, so the
    # rows are compared by what they say rather than by surrogate id.
    assert _rows(task) == expected_rows


class Crash(BaseException):
    """A process dying mid-chunk: nothing in the code may catch it."""


def _rows(task: int) -> dict:
    return {
        "receipts": db.query(
            "SELECT custom_id, outcome FROM batch_result_receipts WHERE task_id=%s "
            "AND consumed_at IS NOT NULL ORDER BY provider_batch_id, custom_id",
            (task,),
        ),
        "verdicts": db.query(
            "SELECT q.url, q.status, q.reason, q.parsed_json, q.error, q.model, q.batch_id, "
            "q.prompt_tokens, q.completion_tokens, q.cached_tokens, q.reasoning_tokens, "
            "q.cost_usd, q.filter_name, q.prompt_hash, q.reasoning_effort, q.input_content, "
            "t.instructions FROM ai_queries q "
            "LEFT JOIN ai_instruction_texts t ON t.id=q.instructions_id "
            "WHERE q.check_type='custom' ORDER BY q.url"
        ),
        "usage": db.query(
            "SELECT model, prompt_tokens, completion_tokens, cached_tokens, cost_usd "
            "FROM model_calls ORDER BY id"
        ),
    }


@pytest.mark.asyncio
async def test_a_crash_mid_chunk_replays_exactly_once(f, monkeypatch):
    _reset_to_empty()
    task, ledger, results = _scenario(f, 13)
    await _collect(task, ledger, results)
    clean = _rows(task)
    assert len(clean["verdicts"]) == 12

    _reset_to_empty()
    task, ledger, results = _scenario(f, 13)
    before = _rows(task)
    hooks = ledger.hooks()
    seen = []

    def crashing(usage, model, batched):
        seen.append(usage)
        if len(seen) == 5:
            raise Crash
        hooks.record_usage(usage, model, batched)

    async def collect(*_args):
        return list(results)

    with pytest.raises(Crash):
        await filter_execution.execute_batch(
            task,
            None,
            SNAPSHOT,
            [],
            replace(hooks, record_usage=crashing),
            contents={},
            unavailable=0,
            collect=collect,
        )
    # The crashed chunk left nothing: not a verdict, a ledger row or a receipt.
    assert _rows(task) == before

    # The worker retries: replay is whatever is still unconsumed.
    await _collect(task, ledger, batch_results.unconsumed(task))
    assert _rows(task) == clean
    # A second replay finds nothing to do and changes nothing.
    await _collect(task, ledger, results)
    assert _rows(task) == clean


class LatencyProxy:
    """A TCP hop that delivers the database's replies `delay` seconds late.

    Delivery is scheduled per reply, not serialized, so a pipelined burst
    costs one delay, as it does over a real link.
    """

    def __init__(self, host: str, port: int, delay: float):
        self.delay = delay
        self.upstream = (host, port)
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.upstream)
            for sock in (client, upstream):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._pump, args=(client, upstream, 0), daemon=True).start()
            threading.Thread(
                target=self._pump, args=(upstream, client, self.delay), daemon=True
            ).start()

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket, delay: float) -> None:
        due: queue.Queue = queue.Queue()

        def deliver() -> None:
            while True:
                at, data = due.get()
                if data is None:
                    sink.close()
                    return
                time.sleep(max(0.0, at - time.monotonic()))
                try:
                    sink.sendall(data)
                except OSError:
                    return

        threading.Thread(target=deliver, daemon=True).start()
        while True:
            try:
                data = source.recv(65536)
            except OSError:
                data = b""
            due.put((time.monotonic() + delay, data or None))
            if not data:
                return

    def close(self) -> None:
        self.server.close()


@contextlib.contextmanager
def _through(proxy: LatencyProxy, monkeypatch):
    dsn = make_conninfo(os.environ["DATABASE_URL"], host="127.0.0.1", port=proxy.port)
    slow = ConnectionPool(dsn, min_size=1, max_size=2, kwargs={"row_factory": dict_row})
    slow.wait()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(core.pool, "pool", slow)
            yield
    finally:
        slow.close()


async def _timed(f, monkeypatch, n: int, delay: float) -> float:
    _reset_to_empty()
    task, ledger, results = _scenario(f, n)
    target = conninfo_to_dict(os.environ["DATABASE_URL"])
    proxy = LatencyProxy(
        str(target.get("host") or "127.0.0.1"), int(target.get("port") or 5432), delay
    )
    try:
        with _through(proxy, monkeypatch):
            started = time.monotonic()
            await _collect(task, ledger, results)
            return time.monotonic() - started
    finally:
        proxy.close()


@pytest.mark.asyncio
async def test_round_trips_per_chunk_do_not_grow_with_its_size(f, monkeypatch):
    """Forty more results in the chunk may not cost forty more round trips."""
    delay = 0.025
    small = await _timed(f, monkeypatch, 10, delay)
    large = await _timed(f, monkeypatch, 50, delay)
    # Per result this was ~9 round trips: 40 more results would be ~9 s here.
    assert large - small < 8 * delay, (small, large)
    assert large < 40 * delay, large
