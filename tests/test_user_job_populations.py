from __future__ import annotations

import datetime

from api import db

ENDPOINT = "/v1/admin/user-job-populations"


def _legacy(user_id, job_id, *, touched=False, notes=None, hidden=False):
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, person_touched_at, notes, hidden) "
        "VALUES (%s, %s, CASE WHEN %s THEN now() END, %s, %s)",
        (user_id, job_id, touched, notes, hidden),
    )


def _working(user_id, job_id):
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)",
        (user_id, job_id),
    )


def _visible(user_id, job_id, at):
    db.execute(
        "INSERT INTO board_visible (user_id, job_id, computed_at) VALUES (%s, %s, %s)",
        (user_id, job_id, at),
    )


def test_populations_are_distinct_pair_sets_with_explicit_uncertainty(client, admin_headers, f):
    source = f.make_source("alpha")
    user_id = f.make_user()
    early = datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)
    late = datetime.datetime(2026, 2, 3, tzinfo=datetime.UTC)

    def job():
        return f.make_job(source=source)

    person_only, working_only, visible_only = job(), job(), job()
    all_three, person_working, person_visible, working_visible = job(), job(), job(), job()
    unknown, default_working, shaped_unmarked, hidden_person = job(), job(), job(), job()
    _legacy(user_id, person_only, touched=True)
    _working(user_id, working_only)
    _visible(user_id, visible_only, early)
    _legacy(user_id, all_three, touched=True)
    _working(user_id, all_three)
    _visible(user_id, all_three, late)
    _legacy(user_id, person_working, touched=True)
    _working(user_id, person_working)
    _legacy(user_id, person_visible, touched=True)
    _visible(user_id, person_visible, early)
    _working(user_id, working_visible)
    _visible(user_id, working_visible, late)
    _legacy(user_id, unknown)
    _legacy(user_id, default_working)
    _working(user_id, default_working)
    _legacy(user_id, shaped_unmarked, notes="person-shaped")
    _legacy(user_id, hidden_person, touched=True, hidden=True)

    response = client.get(ENDPOINT, headers=admin_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert datetime.datetime.fromisoformat(body["generated_at"])
    assert body["basis"] == "distinct user_id/job_id pairs"
    assert "not projection freshness" in body["visibility_basis"]
    expected = {
        "legacy_compatibility": 8,
        "person_state": 5,
        "working_set": 5,
        "materialized_visibility": 4,
        "person_and_working_set": 2,
        "person_and_visibility": 2,
        "working_set_and_visibility": 2,
        "all_three": 1,
        "person_shaped_unmarked": 1,
        "legacy_unknown": 1,
    }
    assert {key: body["global"][key] for key in expected} == expected
    assert body["global"]["source"] is None
    assert body["global"]["visibility_computed_at_min"] == early.isoformat().replace("+00:00", "Z")
    assert body["global"]["visibility_computed_at_max"] == late.isoformat().replace("+00:00", "Z")
    assert body["by_source"] == [{**body["global"], "source": "alpha"}]


def test_filters_count_pairs_and_sources_in_deterministic_order(client, admin_headers, f):
    alpha, beta = f.make_source("alpha"), f.make_source("beta")
    first, second = f.make_user(), f.make_user()
    shared = f.make_job(source=beta)
    alpha_job = f.make_job(source=alpha)
    for user_id in (first, second):
        _working(user_id, shared)
    _working(first, alpha_job)

    all_rows = client.get(ENDPOINT, headers=admin_headers).json()
    assert [row["source"] for row in all_rows["by_source"]] == ["alpha", "beta"]
    assert all_rows["global"]["working_set"] == 3
    assert all_rows["by_source"][1]["working_set"] == 2

    selected = client.get(
        ENDPOINT,
        params={"user": str(first), "source": "beta,alpha"},
        headers=admin_headers,
    ).json()
    assert selected["global"]["working_set"] == 2
    assert [row["source"] for row in selected["by_source"]] == ["alpha", "beta"]
    assert selected["filters"] == {"user": [str(first)], "source": ["beta", "alpha"]}
    assert selected["filterable"] == ["user", "source"]


def test_empty_populations_are_computed_zeros_without_false_freshness(client, admin_headers):
    body = client.get(ENDPOINT, headers=admin_headers).json()

    assert body["global"] == {
        "source": None,
        "legacy_compatibility": 0,
        "person_state": 0,
        "working_set": 0,
        "materialized_visibility": 0,
        "person_and_working_set": 0,
        "person_and_visibility": 0,
        "working_set_and_visibility": 0,
        "all_three": 0,
        "person_shaped_unmarked": 0,
        "legacy_unknown": 0,
        "visibility_computed_at_min": None,
        "visibility_computed_at_max": None,
    }
    assert body["by_source"] == []


def test_population_report_requires_admin(client, user_headers):
    assert client.get(ENDPOINT, headers=user_headers).status_code == 403
