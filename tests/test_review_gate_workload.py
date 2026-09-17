"""Bounded synthetic workload measurements, not production speedup claims."""

from __future__ import annotations

import json
import time
from collections import Counter

import pytest

from api import ai, db, review_gate
from core import batch


@pytest.fixture
def no_paid_calls(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("read models and admission must not make paid calls")

    monkeypatch.setattr(ai, "parse", forbidden)
    monkeypatch.setattr(batch, "submit_batches", forbidden)
    monkeypatch.setattr(batch, "run_responses_batch", forbidden)


def test_report_1000_decision_workload(client, admin_headers, monkeypatch, request, no_paid_calls):
    # Half of the independent fixture belongs to another prompt. Of the
    # selected half, 250 reviews have 300 outcomes, including 50 retries.
    # Wide evidence ensures the report does not return posting bodies.
    db.execute(
        "INSERT INTO review_gate_decisions "
        "(task_id,url,prompt_hash,stage,mode,action,title,policy,evidence) "
        "SELECT 1,'https://workload.test/'||n,CASE WHEN n<500 THEN 'selected' ELSE 'other' END, "
        "CASE WHEN n%%2=0 THEN 'detailed' ELSE 'title' END, "
        "CASE WHEN n%%2=0 THEN 'off' ELSE 'enforce' END, "
        "CASE WHEN n%%2=0 THEN 'review' ELSE 'skip' END,'Engineer','{}', "
        "jsonb_build_object('planned_model','fixture-model','transport','batch', "
        "'profile_input_content',repeat(md5(n::text),128)) "
        "FROM generate_series(0,999) n",
        (),
    )
    db.execute(
        "INSERT INTO review_gate_outcomes "
        "(decision_id,query_id,model,rejected,outcome,recorded_cost_usd,usage) "
        "SELECT id,id,'fixture-model',false,'written',0.001,'{}' "
        "FROM review_gate_decisions WHERE prompt_hash='selected' AND action='review'"
    )
    db.execute(
        "INSERT INTO review_gate_outcomes "
        "(decision_id,query_id,model,rejected,outcome,recorded_cost_usd,usage) "
        "SELECT id,id+1000,'fixture-model',NULL,'failed',0.002,'{}' "
        "FROM review_gate_decisions WHERE prompt_hash='selected' "
        "AND split_part(url,'/',4)::int%%10=0",
        (),
    )
    captured = []
    query_one, query_as = db.query_one, db.query_as

    def one(sql, params=None):
        if "review_gate_decisions" in sql:
            captured.append((sql, params))
        return query_one(sql, params)

    def typed(shape, sql, params=None):
        if "review_gate_decisions" in sql:
            captured.append((sql, params))
        return query_as(shape, sql, params)

    monkeypatch.setattr(db, "query_one", one)
    monkeypatch.setattr(db, "query_as", typed)
    started = time.perf_counter()
    response = client.get(
        "/v1/admin/review-gates/report?prompt_hash=selected", headers=admin_headers
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 200, response.text
    body = response.json()
    assert sum(row["decisions"] for row in body["rows"]) == 500
    paid = next(row for row in body["rows"] if row["action"] == "review")
    assert paid["decisions"] == 250
    assert paid["recorded_outcomes"] == 300
    assert paid["actual_cost_usd"] == pytest.approx(0.35)
    assert body["avoided_cost"]["estimated_avoided_cost_usd"] == pytest.approx(0.35)
    assert body["avoided_cost"]["reference_outcomes"] == 300
    assert "profile_input_content" not in response.text
    # One coverage read and two aggregates, independent of posting count.
    assert len(captured) == 3
    plans = []
    for sql, params in captured:
        if "WITH cohort AS MATERIALIZED" in sql:
            projection = sql.split("FROM review_gate_decisions", 1)[0]
            assert "d.*" not in projection
            assert "d.policy" not in projection
            assert "d.evidence," not in projection
        explained = query_one("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        assert explained
        plan = explained["QUERY PLAN"][0]
        plans.append(
            {
                "execution_ms": plan["Execution Time"],
                "planning_ms": plan["Planning Time"],
                "root_actual_rows": plan["Plan"]["Actual Rows"],
                "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
                "temp_written_blocks": plan["Plan"].get("Temp Written Blocks", 0),
            }
        )
    request.node.user_properties.append(
        (
            "review_report_workload",
            json.dumps(
                {
                    "fixture": "synthetic, 1000 decisions, 500 selected, 300 outcomes, 4096-character evidence",
                    "report_queries": len(captured),
                    "request_ms": elapsed_ms,
                    "plans": plans,
                    "paid_calls": 0,
                }
            ),
        )
    )


def test_partition_500_decisions_uses_bulk_admission(monkeypatch, request, no_paid_calls):
    task = db.query_one(
        "INSERT INTO tasks(kind,payload) VALUES('run_filter_batch_chunk','{}') RETURNING id"
    )
    assert task
    jobs = [
        {"url": f"https://admission.test/{n}", "title": "Engineer", "company": "Example"}
        for n in range(500)
    ]
    contents = {job["url"]: "Description" for job in jobs}
    calls = Counter()
    bulk_rows = []
    for name in ("query", "query_one", "execute", "executemany"):
        original = getattr(db, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            if _name == "executemany":
                bulk_rows.append(len(args[1]))
            return _original(*args, **kwargs)

        monkeypatch.setattr(db, name, counted)
    started = time.perf_counter()
    kept, decisions = review_gate.partition(
        task["id"], "fixture", jobs, contents, model="fixture-model", transport="batch"
    )
    first_ms = (time.perf_counter() - started) * 1000
    initial_calls = dict(calls)
    assert len(kept) == len(decisions) == 500
    assert bulk_rows == [500]
    # The disabled/default policy has bounded reads plus one bulk write, not
    # one database helper call per posting. Network round trips differ from
    # helper calls, so this deliberately reports only the latter.
    assert calls == {"query": 4, "query_one": 2, "executemany": 1, "execute": 1}
    calls.clear()
    started = time.perf_counter()
    replay = review_gate.partition(
        task["id"], "fixture", jobs, contents, model="fixture-model", transport="batch"
    )
    replay_ms = (time.perf_counter() - started) * 1000
    assert replay == (kept, decisions)
    assert calls == {"query": 1}
    request.node.user_properties.append(
        (
            "review_partition_workload",
            json.dumps(
                {
                    "fixture": "synthetic, 500 detailed admissions, default policy, no profile lookup",
                    "initial_db_helper_calls": initial_calls,
                    "bulk_insert_rows": bulk_rows,
                    "replay_db_helper_calls": dict(calls),
                    "initial_ms": first_ms,
                    "replay_ms": replay_ms,
                    "paid_calls": 0,
                }
            ),
        )
    )
