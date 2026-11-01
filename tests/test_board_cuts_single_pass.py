"""/user/stats and /requirements/market each read their slice once now,
where they used to read it once per cut. The cuts must come out exactly as
the per-cut SQL produced them, so the old SQL is kept here as the reference
and the two are compared whole.

The fixtures give every cut distinct counts, so each old ORDER BY fixed one
order and the comparison includes it.
"""

from __future__ import annotations

import datetime
from typing import Any

from api import db
from api.board import populations, visibility
from api.mail import applications
from api.routers import requirements
from api.routers.stats import _BOARD
from core.derivations.requirements import (
    CLEARANCE_LEVELS,
    DEGREE_LEVELS,
    EMPLOYMENT_TYPES,
    SENIORITIES,
)
from tests.job_board_support import _insert_job, _pass_closed, _subscribe, _uid


def _old_stats(uid: int) -> dict[str, Any]:
    params = {"uid": uid}
    by_status = db.query(
        f"""
        SELECT COALESCE(v.status, '') AS status, COUNT(*) AS count
        FROM ({_BOARD}) v WHERE NOT COALESCE(v.hidden, FALSE)
        GROUP BY 1 ORDER BY count DESC
        """,
        params,
    )
    by_source = db.query(
        f"""
        SELECT v.source, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE v.status IS NOT NULL AND v.status != '') AS with_status,
               COUNT(*) FILTER (WHERE v.date_applied IS NOT NULL) AS applied
        FROM ({_BOARD}) v WHERE NOT COALESCE(v.hidden, FALSE)
        GROUP BY v.source ORDER BY total DESC
        """,
        params,
    )
    by_source_status = db.query(
        f"""
        SELECT v.source, COALESCE(v.status, '') AS status, COUNT(*) AS count
        FROM ({_BOARD}) v WHERE NOT COALESCE(v.hidden, FALSE)
        GROUP BY v.source, v.status ORDER BY v.source, count DESC
        """,
        params,
    )
    totals = db.query_one(
        f"""
        SELECT counts.visible, counts.acted_on, legacy.*
        FROM (SELECT {populations.per_user_counts("%(uid)s")}) counts,
             (SELECT COUNT(*) FILTER (WHERE {applications.applied_on("uj")} IS NOT NULL) AS applied,
                     COUNT(*) FILTER (WHERE uj.hidden) AS hidden
              FROM user_jobs uj WHERE uj.user_id = %(uid)s) legacy
        """,
        params,
    )
    assert totals
    return {
        "totals": {k: totals[k] for k in ("visible", "acted_on", "applied", "hidden")},
        "by_status": by_status,
        "by_source": by_source,
        "by_source_status": by_source_status,
    }


def test_stats_cuts_match_the_per_cut_sql(client, user_headers):
    uid = _uid(user_headers)
    applied_at = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
    # (source, user_jobs status or "no row", hidden, applied). NULL and ''
    # both appear in one source: the per-source cut lists them apart and the
    # status cut merges them.
    rows = [
        *[("src-sa", "no row", False, False)] * 3,
        ("src-sa", "", False, False),
        *[("src-sa", "Applied", False, True)] * 2,
        ("src-sa", None, True, False),
        *[("src-sb", "no row", False, False)] * 2,
        ("src-sb", "Interview", False, False),
    ]
    for source in ("src-sa", "src-sb"):
        _subscribe(uid, source)
    for i, (source, status, hidden, applied) in enumerate(rows):
        url = f"https://x.test/single-pass-{i}"
        job_id = _insert_job(source, url)
        _pass_closed(url)
        if status != "no row":
            db.execute(
                "INSERT INTO user_jobs (user_id, job_id, status, hidden) VALUES (%s, %s, %s, %s)",
                (uid, job_id, status, hidden),
            )
        if applied:
            db.execute(
                "INSERT INTO applications (user_id, job_id, company_name, source_provenance, "
                "applied_at) VALUES (%s, %s, 'Acme', 'tracker', %s)",
                (uid, job_id, applied_at),
            )
    visibility.recompute(uid)

    old = _old_stats(uid)
    assert [r["status"] for r in old["by_source_status"]].count("") == 3
    body = client.get("/v1/user/stats", headers=user_headers).json()
    assert {k: body[k] for k in old} == old


def _old_market(uid: int, seniority: str | None, employment_type: str | None) -> dict[str, Any]:
    params = {"uid": uid, "seniority": seniority, "employment_type": employment_type}
    sql = requirements._slice_sql
    postings = db.query_one(sql("SELECT COUNT(*) AS postings FROM slice"), params)
    assert postings
    yoe = db.query(
        sql(
            "SELECT yoe_min AS years, COUNT(*) AS postings FROM slice "
            "WHERE yoe_min IS NOT NULL GROUP BY yoe_min ORDER BY yoe_min"
        ),
        params,
    )
    degree = db.query(
        sql(
            """
            SELECT degree_min AS level, degree_required AS required, COUNT(*) AS postings
            FROM slice WHERE degree_min IS NOT NULL
            GROUP BY degree_min, degree_required
            ORDER BY array_position(%(degrees)s::text[], degree_min), required
            """
        ),
        {**params, "degrees": list(DEGREE_LEVELS)},
    )
    clearance = db.query(
        sql(
            "SELECT clearance AS level, COUNT(*) AS postings FROM slice "
            "WHERE clearance IS NOT NULL GROUP BY clearance "
            "ORDER BY array_position(%(clearances)s::text[], clearance)"
        ),
        {**params, "clearances": list(CLEARANCE_LEVELS)},
    )
    seniorities = db.query(
        sql(
            "SELECT seniority AS level, COUNT(*) AS postings FROM slice "
            "WHERE seniority IS NOT NULL GROUP BY seniority "
            "ORDER BY array_position(%(seniorities)s::text[], seniority)"
        ),
        {**params, "seniorities": list(SENIORITIES)},
    )
    employment = db.query(
        sql(
            "SELECT employment_type AS type, COUNT(*) AS postings FROM slice "
            "WHERE employment_type IS NOT NULL GROUP BY employment_type "
            "ORDER BY array_position(%(types)s::text[], employment_type)"
        ),
        {**params, "types": list(EMPLOYMENT_TYPES)},
    )
    flags = db.query_one(
        sql(
            """
            SELECT COUNT(*) FILTER (WHERE yoe_min IS NOT NULL) AS states_years,
                   COUNT(*) FILTER (WHERE degree_min IS NOT NULL) AS states_degree,
                   COUNT(*) FILTER (WHERE degree_required) AS degree_required,
                   COUNT(*) FILTER (WHERE enrollment_required) AS enrollment_required,
                   COUNT(*) FILTER (WHERE clearance IS NOT NULL
                                      AND clearance != 'none') AS needs_clearance,
                   COUNT(*) FILTER (WHERE citizenship_required) AS citizenship_required,
                   COUNT(*) FILTER (WHERE sponsorship = 'offered') AS sponsorship_offered,
                   COUNT(*) FILTER (WHERE sponsorship = 'not_offered') AS sponsorship_refused
            FROM slice
            """
        ),
        params,
    )
    return {
        "postings": postings["postings"],
        "years_experience": {"stated": sum(r["postings"] for r in yoe), "distribution": yoe},
        "degree": degree,
        "clearance": clearance,
        "seniority": seniorities,
        "employment_type": employment,
        "flags": flags,
    }


def test_market_cuts_match_the_per_cut_sql(client, user_headers, f):
    uid = _uid(user_headers)
    source = f.make_source()
    f.subscribe(uid, source)
    # Counts differ within every cut, and every field has a stated and an
    # unstated row, so each ORDER BY and each FILTER is exercised.
    specs: list[dict[str, Any]] = [
        *[{"yoe_min": 2, "seniority": "entry", "employment_type": "full_time"}] * 3,
        *[{"yoe_min": 0, "seniority": "entry", "employment_type": "intern"}] * 2,
        {"yoe_min": 5, "seniority": "senior", "employment_type": "full_time"},
        *[{"degree_min": "bachelors", "degree_required": True, "clearance": "none"}] * 3,
        *[{"degree_min": "bachelors", "degree_required": False, "clearance": "secret"}] * 2,
        {"degree_min": "masters", "degree_required": False, "enrollment_required": True},
        *[{"citizenship_required": True, "sponsorship": "not_offered"}] * 2,
        {"sponsorship": "offered", "clearance": "public_trust"},
    ]
    for spec in specs:
        _, url = f.make_ready_job(source=source, content="a long job description " * 20)
        f.make_requirements(url, skills_required=["Python"], **spec)
    visibility.recompute(uid)

    for query in ({}, {"seniority": "entry"}, {"employment_type": "full_time"}):
        old = _old_market(uid, query.get("seniority"), query.get("employment_type"))
        assert old["postings"] > 0
        body = client.get("/v1/requirements/market", headers=user_headers, params=query).json()
        assert {k: body[k] for k in old} == old
