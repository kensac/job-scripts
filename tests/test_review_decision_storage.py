"""Every read surface resolves a decision through its URL and body references."""

import pytest

from api import db, review_gate, review_gate_reads, review_gate_records
from tests.test_review_gate import configure
from tests.test_review_gate_reads import outcome


def admitted(f):
    """Real admissions: a skip, a review with routing evidence, two tasks, outcomes."""
    configure()
    user = f.make_user()
    first = f.make_task("run_filter_batch_chunk", {"user_id": user, "filter_id": 5})
    second = f.make_task("run_filter_batch_chunk", {"user_id": user, "filter_id": 5})
    jobs = [
        {"url": "https://example.test/nurse", "title": "Registered Nurse", "company": "Care"},
        {"url": "https://example.test/eng", "title": "Software Engineer", "company": "Example"},
    ]
    contents = {job["url"]: f"content {job['title']}" for job in jobs}
    for task in (first, second):
        review_gate.partition(
            task, "test-hash", jobs, contents, model="test-model", transport="batch"
        )
    db.execute(
        "UPDATE review_gate_decision_bodies b SET evidence=b.evidence||"
        '\'{"routing":{"outcome":"reject","would_review":false}}\'::jsonb '
        "FROM review_gate_decisions d JOIN review_gate_urls u ON u.id=d.url_id "
        "WHERE b.id=d.body_id AND u.url='https://example.test/eng'"
    )
    decisions = db.query(
        "SELECT d.id,b.stage FROM review_gate_decisions d "
        "JOIN review_gate_decision_bodies b ON b.id=d.body_id ORDER BY d.id"
    )
    for n, row in enumerate(decisions):
        outcome(row["id"], 100 + n, 0.25, rejected=row["stage"] == "title")
    return user, (first, second), jobs, contents


def observed(client, admin_headers, user_headers, tasks, jobs, contents, job_id):
    first = tasks[0]
    admin = "/v1/admin/review-gates"
    pages = {
        "all": f"{admin}/decisions",
        "url": f"{admin}/decisions?url=https://example.test/eng",
        "stage": f"{admin}/decisions?stage=title&mode=enforce&action=skip",
        "prompt": f"{admin}/decisions?prompt_hash=test-hash&page_size=1&page=2",
        "missing": f"{admin}/decisions?prompt_hash=other",
        "report": f"{admin}/report?days=7",
        "report_prompt": f"{admin}/report?prompt_hash=test-hash",
        "timeline": "/v1/admin/jobs/timeline?url=https://example.test/nurse",
    }
    result = {name: client.get(path, headers=admin_headers).json() for name, path in pages.items()}
    for name in ("report", "report_prompt"):
        result[name].pop("generated_at")
        result[name].pop("window_start")
        result[name].pop("window_end")
    result["personal"] = client.get(
        f"/v1/user/jobs/{job_id}/review-decisions", headers=user_headers
    ).json()
    result["existing"] = review_gate_records.existing(first)
    result["exclusions"] = review_gate_records.exclusions(first, "test-hash")
    result["comparisons"] = review_gate_reads.comparisons(first)
    result["replay"] = review_gate.partition(
        first, "test-hash", jobs, contents, model="test-model", transport="batch"
    )
    return result


@pytest.fixture
def owned_job(user_headers):
    from tests.test_api_jobs import _insert_job, _uid

    me = _uid(user_headers)
    return me, _insert_job("manual", "https://example.test/eng", uploaded_by=me)


def baseline(f, client, admin_headers, user_headers, owned_job):
    """Every read surface over reference-only rows, the shape production holds."""
    me, job_id = owned_job
    _, tasks, jobs, contents = admitted(f)
    db.execute("UPDATE review_gate_decisions SET user_id=%s", (me,))
    before = observed(client, admin_headers, user_headers, tasks, jobs, contents, job_id)
    assert before["all"]["total"] == 4
    assert {row["title"] for row in before["all"]["rows"]} == {
        "Registered Nurse",
        "Software Engineer",
    }
    assert before["url"]["total"] == 2
    assert before["stage"]["total"] == 2
    assert before["timeline"]["decisions"]["total"] == 2
    assert before["personal"]["total"] == 2
    assert before["existing"]["https://example.test/nurse"]["policy"]["title_mode"] == "enforce"
    assert before["exclusions"] == {"https://example.test/nurse"}
    assert before["comparisons"]["routing"] and before["comparisons"]["review_gate"]
    return tasks, jobs, contents, job_id, before


def test_every_read_surface_resolves_the_references(
    f, client, admin_headers, user_headers, owned_job
):
    tasks, jobs, contents, job_id, before = baseline(
        f, client, admin_headers, user_headers, owned_job
    )
    # A URL no decision references matches nothing.
    response = client.get(
        "/v1/admin/review-gates/decisions",
        params={"url": "https://example.test/unrecorded"},
        headers=admin_headers,
    )
    assert response.json()["total"] == 0
    assert observed(client, admin_headers, user_headers, tasks, jobs, contents, job_id) == before


def test_a_row_cannot_lose_a_reference(f):
    _, tasks, _, _ = admitted(f)
    from psycopg.errors import NotNullViolation

    for column in ("url_id", "body_id"):
        with pytest.raises(NotNullViolation):
            db.execute(
                f"UPDATE review_gate_decisions SET {column}=NULL WHERE task_id=%s", (tasks[0],)
            )


def test_task_url_uniqueness_holds_for_reference_only_rows(f):
    _, tasks, _, _ = admitted(f)
    from psycopg.errors import UniqueViolation

    with pytest.raises(UniqueViolation):
        db.execute(
            "INSERT INTO review_gate_decisions(task_id,url_id,body_id) "
            "SELECT task_id,url_id,body_id FROM review_gate_decisions WHERE task_id=%s LIMIT 1",
            (tasks[0],),
        )
