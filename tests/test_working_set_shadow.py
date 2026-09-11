from __future__ import annotations

from api import db
from api.board import working_set_shadow


def _comparison(body: dict, name: str) -> dict:
    return body[name]


def test_shadow_report_separates_known_unknown_and_proposed_scope(client, admin_headers, f):
    source = f.make_source()
    both_user = f.make_user()
    unknown_user = f.make_user()
    proposed_user = f.make_user()
    both_job = f.make_job(source=source)
    unknown_job = f.make_job(source=source)
    proposed_job = f.make_job(source=source)

    db.execute(
        "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s), (%s, %s)",
        (both_user, both_job, unknown_user, unknown_job),
    )
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s), (%s, %s)",
        (both_user, both_job, proposed_user, proposed_job),
    )

    response = client.get("/v1/admin/working-set-shadow", headers=admin_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    expected = {
        "old_total": 2,
        "proposed_total": 2,
        "both": 1,
        "old_only": 1,
        "proposed_only": 1,
        "legacy_unknown": 1,
        "old_only_known": 0,
    }
    for surface in (
        "pair_membership",
        "ai_eligible_jobs",
        "stale_reverify_candidates",
        "scheduled_users",
    ):
        got = _comparison(body, surface)
        assert {key: got[key] for key in expected} == expected
        assert got["sample_old_only_known"] == []
        assert len(got["sample_proposed_only"]) == 1
        assert len(got["sample_legacy_unknown"]) == 1

    users = {row["user_id"]: row for row in body["users"]}
    assert users[unknown_user]["pair_membership"]["legacy_unknown"] == 1
    assert users[proposed_user]["pair_membership"]["proposed_only"] == 1
    assert body["digest_candidates"] == {
        "old_candidates": 2,
        "proposed_candidates": None,
        "cannot_tell": 2,
        "reason": working_set_shadow.DIGEST_UNKNOWN_REASON,
    }
    assert body["reverify_per_cycle"] == working_set_shadow.REVERIFY_PER_CYCLE


def test_independent_eligibility_does_not_turn_unknown_scope_into_a_difference(
    client, admin_headers, f
):
    user_id = f.make_user()
    source = f.make_source()
    f.subscribe(user_id, source)
    job_id = f.make_job(source=source)
    db.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)", (user_id, job_id))

    body = client.get("/v1/admin/working-set-shadow", headers=admin_headers).json()

    eligible = body["ai_eligible_jobs"]
    assert eligible["both"] == 1
    assert eligible["old_only"] == 0
    assert eligible["legacy_unknown"] == 0
    assert body["pair_membership"]["legacy_unknown"] == 1


def test_stale_known_difference_is_not_reported_as_legacy_unknown(client, admin_headers, f):
    user_id = f.make_user()
    source = f.make_source()
    job_id = f.make_job(source=source)
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, person_touched_at) VALUES (%s, %s, now())",
        (user_id, job_id),
    )

    body = client.get("/v1/admin/working-set-shadow", headers=admin_headers).json()

    stale = body["stale_reverify_candidates"]
    assert stale["old_only"] == 1
    assert stale["old_only_known"] == 1
    assert stale["legacy_unknown"] == 0
    assert stale["sample_old_only_known"] == [str(job_id)]
    assert stale["sample_legacy_unknown"] == []


def test_person_shaped_marker_null_row_is_a_known_old_only_difference(client, admin_headers, f):
    user_id = f.make_user()
    source = f.make_source()
    job_id = f.make_job(source=source)
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'Saved')",
        (user_id, job_id),
    )

    body = client.get("/v1/admin/working-set-shadow", headers=admin_headers).json()

    pairs = body["pair_membership"]
    assert pairs["old_only"] == 1
    assert pairs["old_only_known"] == 1
    assert pairs["legacy_unknown"] == 0
    assert pairs["sample_old_only_known"] == [f"{user_id}:{job_id}"]
    assert pairs["sample_legacy_unknown"] == []


def test_shadow_report_is_empty_not_missing_on_an_empty_database(client, admin_headers):
    body = client.get("/v1/admin/working-set-shadow", headers=admin_headers).json()

    for surface in (
        "pair_membership",
        "ai_eligible_jobs",
        "stale_reverify_candidates",
        "scheduled_users",
    ):
        assert body[surface]["old_total"] == 0
        assert body[surface]["proposed_total"] == 0
    assert body["users"] == []
    assert body["digest_candidates"]["proposed_candidates"] is None
    assert body["digest_candidates"]["reason"] == working_set_shadow.DIGEST_UNKNOWN_REASON


def test_shadow_report_requires_an_admin(client, user_headers):
    response = client.get("/v1/admin/working-set-shadow", headers=user_headers)
    assert response.status_code == 403


def test_samples_are_bounded_and_deterministic(client, admin_headers, f):
    user_id = f.make_user()
    source = f.make_source()
    job_ids = [f.make_job(source=source) for _ in range(working_set_shadow.SAMPLE_LIMIT + 5)]
    db.executemany(
        "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)",
        [(user_id, job_id) for job_id in reversed(job_ids)],
    )

    body = client.get("/v1/admin/working-set-shadow", headers=admin_headers).json()
    sample = body["pair_membership"]["sample_legacy_unknown"]
    expected = sorted(f"{user_id}:{job_id}" for job_id in job_ids)[
        : working_set_shadow.SAMPLE_LIMIT
    ]
    assert sample == expected
