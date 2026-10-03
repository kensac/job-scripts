"""Opt-in disposable-database measurements of instruction storage overhead."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from api import db
from core import store

pytestmark = pytest.mark.skipif(
    os.environ.get("INSTRUCTION_BENCHMARK_ROLE") not in ("baseline", "candidate"),
    reason="Only enabled by the isolated instruction benchmark workflow",
)


class Measurements:
    def __init__(self, monkeypatch):
        self.lock = threading.Lock()
        self.active = None
        self.reports = []
        execute = psycopg.Cursor.execute
        executemany = psycopg.Cursor.executemany
        internal = psycopg.Connection._exec_command

        def record(kind, command, count=1, conn=None):
            if isinstance(command, bytes):
                text = command.decode("ascii", errors="replace")
            elif isinstance(command, str):
                text = command
            elif conn is not None and hasattr(command, "as_string"):
                text = command.as_string(conn)
            else:
                text = "COMPOSED"
            operation = text.lstrip().split(None, 1)[0].upper()
            with self.lock:
                if self.active is not None:
                    self.active[kind][operation] += count

        def measured_execute(cursor, query, *args, **kwargs):
            record("cursor_statements", query)
            return execute(cursor, query, *args, **kwargs)

        def measured_executemany(cursor, query, params_seq, *args, **kwargs):
            params_seq = list(params_seq)
            record("cursor_statements", query, len(params_seq))
            return executemany(cursor, query, params_seq, *args, **kwargs)

        def measured_internal(conn, command, *args, **kwargs):
            record("internal_commands", command, conn=conn)
            return (yield from internal(conn, command, *args, **kwargs))

        # Connection.execute delegates to Cursor.execute. Counting at the
        # cursor avoids counting that one statement twice. Internal commands
        # separately capture implicit BEGIN and pool-context COMMIT boundaries.
        monkeypatch.setattr(psycopg.Cursor, "execute", measured_execute)
        monkeypatch.setattr(psycopg.Cursor, "executemany", measured_executemany)
        monkeypatch.setattr(psycopg.Connection, "_exec_command", measured_internal)

    @contextmanager
    def phase(self, name, operations, requests=0):
        report = {
            "phase": name,
            "operations": operations,
            "requests": requests,
            "cursor_statements": Counter(),
            "internal_commands": Counter(),
        }
        with self.lock:
            assert self.active is None
            self.active = report
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            with self.lock:
                self.active = None
            report["elapsed_seconds"] = elapsed
            report["cursor_statement_count"] = sum(report["cursor_statements"].values())
            report["internal_command_count"] = sum(report["internal_commands"].values())
            self.reports.append(report)
        assert report["cursor_statement_count"] > 0
        assert report["internal_commands"]["BEGIN"] > 0
        assert report["internal_commands"]["COMMIT"] > 0


@pytest.mark.parametrize("size", [100, 1000])
def test_instruction_hot_path_workload(size, client, admin_headers, monkeypatch, capsys):
    role = os.environ["INSTRUCTION_BENCHMARK_ROLE"]
    instructions = [f"Rule {index}\n" + "Exact instruction line.\n" * 80 for index in range(10)]
    content = "Posting text.\n" * 400
    shared_url = "https://example.test/benchmark/history"
    urls = [f"https://example.test/benchmark/{index}" for index in range(size)]

    def write(url, index):
        return store.add_ai_result(
            url,
            "passed",
            check_type="custom",
            prompt_hash="benchmark-filter",
            instructions=instructions[index % len(instructions)],
            input_content=content,
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
            cached_tokens=1,
        )

    # The measured write case exercises a warmed ten-value dictionary. Initial
    # dictionary creation and application/auth startup are outside measurements.
    for index in range(len(instructions)):
        write(f"https://example.test/benchmark/warm/{index}", index)
    meter = Measurements(monkeypatch)
    with meter.phase("write_inline", size):
        ids = [write(url, index) for index, url in enumerate(urls)]
    history_ids = [write(shared_url, index) for index in range(size)]
    tracked_ids = ids + history_ids
    original_accounting = db.query(
        "SELECT id,status,prompt_hash,input_content,prompt_tokens,completion_tokens,"
        "total_tokens,cached_tokens,cost_usd FROM ai_queries WHERE id=ANY(%s) ORDER BY id",
        (tracked_ids,),
    )
    assert len(original_accounting) == 2 * size
    assert all(
        row["status"] == "passed"
        and row["prompt_hash"] == "benchmark-filter"
        and row["input_content"] == content
        and (
            row["prompt_tokens"],
            row["completion_tokens"],
            row["total_tokens"],
            row["cached_tokens"],
        )
        == (10, 2, 12, 1)
        and row["cost_usd"] is None
        for row in original_accounting
    )

    def read_workload(label):
        with meter.phase(f"cache_{label}", size):
            cached = [store.get_custom_result(url, "benchmark-filter") for url in urls]
        assert [row["id"] for row in cached] == ids
        assert [row["instructions"] for row in cached] == [
            instructions[i % 10] for i in range(size)
        ]
        assert all(row["input_content"] == content and row["cost_usd"] is None for row in cached)
        with meter.phase(f"admin_detail_{label}", size, requests=size):
            detail_responses = [
                client.get(f"/v1/admin/queries/{qid}", headers=admin_headers) for qid in ids
            ]
        assert all(response.status_code == 200 for response in detail_responses)
        details = [response.json() for response in detail_responses]
        assert [row["id"] for row in details] == ids
        assert [row["instructions"] for row in details] == [
            instructions[i % 10] for i in range(size)
        ]
        assert all(
            row["input_content"] == content
            and row["prompt_tokens"] == 10
            and row["cost_usd"] is None
            for row in details
        )
        with meter.phase(f"admin_history_{label}", 3 * size, requests=3):
            history_responses = [
                client.get(
                    "/v1/admin/jobs/responses",
                    params={"url": shared_url},
                    headers=admin_headers,
                )
                for _ in range(3)
            ]
        for response in history_responses:
            assert response.status_code == 200
            rows = response.json()["rows"]
            assert [row["id"] for row in rows] == history_ids
            assert [row["instructions"] for row in rows] == [
                instructions[i % 10] for i in range(size)
            ]
            assert all(
                row["input_content"] == content
                and row["total_tokens"] == 12
                and row["cost_usd"] is None
                for row in rows
            )
        return cached, details, history_responses[-1].json()

    inline_results = read_workload("inline")
    if role == "candidate":
        from api.ai.migrate_query_instructions import migrate_chunk

        through = max(tracked_ids)
        for mode in ("copy", "verify", "compact"):
            after = 0
            while after < through:
                result = migrate_chunk(
                    mode=mode,
                    after=after,
                    through=through,
                    limit=100,
                    backup_complete=mode == "compact",
                    readers_compatible=mode == "compact",
                )
                assert result["after"] > after
                after = result["after"]
        compacted = db.query(
            "SELECT id,instructions,instructions_id FROM ai_queries WHERE id=ANY(%s)",
            (tracked_ids,),
        )
        assert len(compacted) == 2 * size
        assert all(
            row["instructions"] is None and row["instructions_id"] is not None for row in compacted
        )
        assert read_workload("compacted") == inline_results
        assert (
            db.query(
                "SELECT id,status,prompt_hash,input_content,prompt_tokens,completion_tokens,"
                "total_tokens,cached_tokens,cost_usd FROM ai_queries WHERE id=ANY(%s) ORDER BY id",
                (tracked_ids,),
            )
            == original_accounting
        )

    report = {
        "role": role,
        "revision": os.environ["TEST_REVISION"],
        "repetition": int(os.environ["INSTRUCTION_BENCHMARK_REPETITION"]),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "psycopg": psycopg.__version__,
        "rows": size,
        "distinct_instructions": len(instructions),
        "instruction_utf8_bytes": [len(value.encode("utf-8")) for value in instructions],
        "content_utf8_bytes": len(content.encode("utf-8")),
        "phases": meter.reports,
        "limitations": [
            "Instrumented disposable CI database and in-process HTTP client; not production throughput.",
            "Cursor counts are executed statements, not network round trips; executemany counts each parameter set.",
            "Internal command hook is specific to the pinned psycopg version; protocol prepare/describe messages are not counted.",
            "HTTP phases include authentication and serialization; seeding, migration, and warm-up are excluded.",
            "No model is selected, so historical unpriced cost remains NULL; token fields and response identity are asserted.",
        ],
    }
    output = Path("results")
    output.mkdir(exist_ok=True)
    path = output / f"instruction-{role}-{report['repetition']}-{size}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    with capsys.disabled():
        print(
            json.dumps({"instruction_benchmark": str(path), "phases": meter.reports}),
            flush=True,
        )
