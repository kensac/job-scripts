"""The report reads stored rows and joins bodies once; its output must equal the
definition it replaced, which joined every decision to its body and URL first.

`_reference` is that definition, kept verbatim as the oracle: one row per
decision through DECISIONS, aggregated directly.
"""

from __future__ import annotations

import datetime
import math
import random
import sys

import pytest

from api import db
from api.review_decision_storage import DECISIONS
from api.routers.admin import review_gates

STAGES = ("title", "profile", "detailed")
MODES = ("off", "shadow", "enforce")
PROMPTS = ("p1", "p2", "p3")
MODELS = ("m", "other")
COSTS = (None, "0.1", "0.2", "0.3", "0.000123456789", "1.7")
AGES = (0, 0.4, 0.99, 1.01, 3, 6.9, 7.1, 20, 89.9, 90.5, 200)


def _fixture() -> None:
    from api import review_policy_storage

    rng = random.Random(20261003)
    policy = review_policy_storage.intern("{}")
    db.execute(
        "INSERT INTO review_gate_urls(url) "
        "SELECT 'https://equivalence.test/'||n FROM generate_series(0,39) n"
    )
    bodies = []
    for n in range(36):
        bodies.append(
            db.query_one(
                "INSERT INTO review_gate_decision_bodies"
                "(digest,prompt_hash,stage,mode,action,title,policy_id,evidence) "
                "VALUES(sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,'Engineer',%s,%s) RETURNING id",
                (
                    f"equivalence-{n}",
                    PROMPTS[n % 3],
                    STAGES[(n // 3) % 3],
                    MODES[(n // 9) % 3],
                    "review" if n % 2 else "skip",
                    policy,
                    # Some bodies carry no transport: a NULL key must match
                    # neither side of the baseline join, as before.
                    db.jsonb(
                        {"planned_model": MODELS[n % 2]}
                        if n % 7 == 0
                        else {"planned_model": MODELS[(n // 2) % 2], "transport": "batch"}
                    ),
                ),
            )["id"]
        )
    urls = [row["id"] for row in db.query("SELECT id FROM review_gate_urls ORDER BY id")]
    query = 0
    # The same body is shared by many tasks and URLs, and a URL by many tasks.
    for task in range(1, 61):
        for url in rng.sample(urls, 12):
            body = rng.choice(bodies)
            # Prompt p3 is fully priced, so its rows have an actual cost and
            # can serve as a savings baseline.
            priced = PROMPTS[bodies.index(body) % 3] == "p3"
            did = db.query_one(
                "INSERT INTO review_gate_decisions"
                "(task_id,url_id,body_id,user_id,managed_board_id,filter_id,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,now()-(%s * interval '1 day')) RETURNING id",
                (
                    task,
                    url,
                    body,
                    rng.choice((None, 1, 2, 3)),
                    rng.choice((None, 10, 11)),
                    rng.choice((None, 20, 21)),
                    rng.choice(AGES),
                ),
            )["id"]
            # Decisions with no outcome, one, and retries.
            for _ in range(rng.choice((1, 2) if priced else (0, 0, 1, 1, 2, 3))):
                query += 1
                db.execute(
                    "INSERT INTO review_gate_outcomes"
                    "(decision_id,query_id,model,rejected,outcome,recorded_cost_usd,usage) "
                    "VALUES(%s,%s,%s,%s,'written',%s,'{}')",
                    (
                        did,
                        query,
                        MODELS[(bodies.index(body) // 2) % 2]
                        if priced
                        else rng.choice((*MODELS, None)),
                        rng.choice((True, False, None)),
                        rng.choice(COSTS[1:] if priced else COSTS),
                    ),
                )


def _reference(days, prompt_hash, user, managed_board_id, filter_id, start, end):
    where, values, _ = review_gates.selection(
        prompt_hash=prompt_hash, user=user, managed_board_id=managed_board_id, filter_id=filter_id
    )
    first = db.query_one(
        f"SELECT min(d.created_at) AS first FROM {DECISIONS} WHERE {where}", values
    )
    cohort = (
        "SELECT d.id,d.url,d.stage,d.mode,d.action,d.prompt_hash,"
        "d.evidence->>'planned_model' planned_model,d.evidence->>'transport' transport "
        f"FROM {DECISIONS} WHERE {where} "
        "AND d.created_at >= %(start)s AND d.created_at < %(end)s"
    )
    bounded = {**values, "start": start, "end": end}
    rows = db.query_as(
        review_gates.GateFunnelRow,
        f"WITH cohort AS MATERIALIZED ({cohort}), paid AS ("
        "SELECT o.decision_id,count(*) n,count(*) FILTER(WHERE o.recorded_cost_usd IS NULL) unknown, "
        "sum(o.recorded_cost_usd) cost, "
        "count(*) FILTER(WHERE o.rejected IS TRUE) rejected, "
        "count(*) FILTER(WHERE o.rejected IS FALSE) passed, "
        "count(*) FILTER(WHERE o.rejected IS NULL) unresolved "
        "FROM review_gate_outcomes o JOIN cohort c ON c.id=o.decision_id GROUP BY o.decision_id) "
        "SELECT d.stage,d.mode,d.action,count(*) AS decisions,count(DISTINCT d.url) AS distinct_jobs, "
        "COALESCE(sum(p.n),0)::bigint recorded_outcomes, "
        "COALESCE(sum(p.unknown),0)::bigint unpriced_outcomes, "
        "count(*) FILTER(WHERE d.action='review' AND p.decision_id IS NULL) without_recorded_outcome, "
        "COALESCE(sum(p.cost),0)::float known_cost_usd, "
        "CASE WHEN COALESCE(sum(p.unknown),0)=0 AND count(*) FILTER(WHERE d.action='review' "
        "AND p.decision_id IS NULL)=0 THEN COALESCE(sum(p.cost),0)::float END actual_cost_usd, "
        "COALESCE(sum(p.rejected) FILTER(WHERE d.mode='shadow' AND d.stage<>'detailed'),0)::bigint agreed_reject, "
        "COALESCE(sum(p.passed) FILTER(WHERE d.mode='shadow' AND d.stage<>'detailed'),0)::bigint false_reject, "
        "COALESCE(sum(p.unresolved) FILTER(WHERE d.mode='shadow' AND d.stage<>'detailed'),0)::bigint unresolved "
        "FROM cohort d LEFT JOIN paid p ON p.decision_id=d.id "
        "GROUP BY d.stage,d.mode,d.action ORDER BY d.stage,d.mode,d.action",
        bounded,
    )
    estimate = db.query_one(
        f"WITH cohort AS MATERIALIZED ({cohort}), per_decision AS ("
        "SELECT d.id,d.prompt_hash,d.planned_model model,d.transport, "
        "sum(o.recorded_cost_usd)::float cost,count(o.id) n, "
        "count(*) FILTER(WHERE o.recorded_cost_usd IS NULL OR o.model IS DISTINCT FROM "
        "d.planned_model) unknown "
        "FROM cohort d LEFT JOIN review_gate_outcomes o ON o.decision_id=d.id "
        "WHERE d.action='review' GROUP BY d.id,d.prompt_hash,d.planned_model,d.transport), baseline AS ("
        "SELECT prompt_hash,model,transport,avg(cost)::float mean_cost,sum(n) n "
        "FROM per_decision GROUP BY prompt_hash,model,transport "
        "HAVING sum(unknown)=0), skipped AS ("
        "SELECT d.prompt_hash,d.planned_model model,d.transport,count(*) n "
        "FROM cohort d WHERE d.action='skip' GROUP BY d.prompt_hash,d.planned_model,d.transport) "
        "SELECT CASE WHEN count(r.mean_cost)>0 THEN sum(s.n*r.mean_cost)::float END estimated_avoided_cost_usd, "
        "COALESCE(sum(s.n) FILTER(WHERE r.mean_cost IS NOT NULL),0)::bigint estimated_decisions, "
        "COALESCE(sum(s.n) FILTER(WHERE r.mean_cost IS NULL),0)::bigint unestimated_decisions, "
        "COALESCE(sum(r.n),0)::bigint reference_outcomes "
        "FROM skipped s LEFT JOIN baseline r USING(prompt_hash,model,transport)",
        bounded,
    )
    return first["first"] if first else None, rows, estimate


SELECTIONS = [
    {},
    {"prompt_hash": "p2"},
    {"prompt_hash": "absent"},
    {"user": "1"},
    {"user": "2,3"},
    {"user": "999"},
    {"managed_board_id": 10},
    {"filter_id": 20},
    {"filter_id": 21, "user": "1,2", "prompt_hash": "p1"},
    {"managed_board_id": 11, "prompt_hash": "p3"},
    {"prompt_hash": "p3"},
]


@pytest.mark.parametrize("timezone", ["UTC", "Pacific/Chatham"])
def test_report_equals_the_per_decision_definition(client, timezone):
    _fixture()
    compared = 0
    for days in (1, 7, 90):
        for chosen in SELECTIONS:
            arguments = {
                "prompt_hash": None,
                "user": None,
                "managed_board_id": None,
                "filter_id": None,
                **chosen,
            }
            with db.transaction():
                db.execute("SELECT set_config('TimeZone', %s, true)", (timezone,))
                report = review_gates._report(days, **arguments)
                first, rows, estimate = _reference(
                    days, **arguments, start=report.window_start, end=report.window_end
                )
            assert report.window_end - report.window_start == datetime.timedelta(days=days)
            assert report.first_recorded_at == first
            assert report.rows == rows, (days, chosen)
            # The avoided cost is a float sum of float means, as before, and
            # PostgreSQL adds floats in the order the plan feeds them, so the
            # last bits follow the plan, not the data. Every other figure is
            # an integer or a numeric sum and must match exactly.
            avoided = report.avoided_cost.model_dump()
            expected = estimate["estimated_avoided_cost_usd"]
            if expected is None:
                assert avoided["estimated_avoided_cost_usd"] is None
            else:
                assert math.isclose(
                    avoided["estimated_avoided_cost_usd"],
                    expected,
                    rel_tol=8 * sys.float_info.epsilon,
                ), (days, chosen)
            assert {**avoided, "estimated_avoided_cost_usd": None} == {
                **estimate,
                "estimated_avoided_cost_usd": None,
                "basis": report.avoided_cost.basis,
            }, (days, chosen)
            compared += len(rows)
    # The fixture reaches every shape the columns distinguish, so equality is
    # not over empty or trivial groups.
    totals = db.query_one(
        "SELECT count(DISTINCT (stage,mode,action)) groups, count(*) decisions, "
        "count(*) FILTER(WHERE NOT EXISTS(SELECT 1 FROM review_gate_outcomes o "
        f"WHERE o.decision_id=d.id)) unpaid FROM {DECISIONS}"
    )
    assert totals["groups"] == 18
    assert totals["unpaid"] > 0
    assert compared > 200
    unfiltered = review_gates._report(90, None, None, None, None)
    assert any(row.actual_cost_usd is None for row in unfiltered.rows)
    priced = review_gates._report(90, "p3", None, None, None)
    assert all(row.actual_cost_usd is not None for row in priced.rows)
    assert any(row.agreed_reject and row.false_reject for row in unfiltered.rows)
    assert any(row.distinct_jobs < row.decisions for row in unfiltered.rows)
    assert unfiltered.avoided_cost.estimated_decisions > 0
    assert unfiltered.avoided_cost.unestimated_decisions > 0


def test_stored_row_joins_cannot_add_or_drop_a_decision(client):
    """The report reads review_gate_decisions without joining both references.

    That equals reading DECISIONS only while each reference is NOT NULL under a
    validated foreign key to a primary key, which this checks on the schema.
    """
    rows = db.query(
        "SELECT a.attname,a.attnotnull,c.convalidated,c.confrelid::regclass::text target "
        "FROM pg_attribute a LEFT JOIN pg_constraint c ON c.conrelid=a.attrelid "
        "AND c.contype='f' AND c.conkey=ARRAY[a.attnum] "
        "WHERE a.attrelid='review_gate_decisions'::regclass AND a.attname IN ('url_id','body_id') "
        "ORDER BY a.attname"
    )
    assert rows == [
        {
            "attname": "body_id",
            "attnotnull": True,
            "convalidated": True,
            "target": "review_gate_decision_bodies",
        },
        {
            "attname": "url_id",
            "attnotnull": True,
            "convalidated": True,
            "target": "review_gate_urls",
        },
    ]
    assert (
        db.query_one(
            "SELECT count(*) n FROM pg_index WHERE indisunique "
            "AND indrelid='review_gate_urls'::regclass AND indkey::text="
            "(SELECT attnum::text FROM pg_attribute WHERE attrelid='review_gate_urls'::regclass "
            "AND attname='url')"
        )["n"]
        == 1
    )
