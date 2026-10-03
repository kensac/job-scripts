"""Every reader returns the same decision whichever shape stores it.

Reference-only rows are built here with raw SQL rather than by the migration
under test, so a reader and the backfill cannot agree with each other by
sharing a mistake.
"""

import pytest

from api import db, review_gate, review_gate_reads, review_gate_records
from tests.test_review_gate import configure
from tests.test_review_gate_reads import outcome

INLINE = (
    "url",
    "prompt_hash",
    "stage",
    "mode",
    "action",
    "reason",
    "profile_id",
    "title",
    "content_hash",
    "evidence",
)


def reference_only(ids=None):
    """Move the selected rows to the body-and-url shape, clearing inline copies."""
    scope = {"ids": ids, "all": ids is None}
    selected = "(%(all)s OR d.id=ANY(%(ids)s::bigint[]))"
    db.execute(
        "INSERT INTO review_gate_urls(url) SELECT DISTINCT d.url FROM review_gate_decisions d "
        f"WHERE d.url IS NOT NULL AND {selected} ON CONFLICT(url) DO NOTHING",
        scope,
    )
    db.execute(
        "INSERT INTO review_gate_decision_bodies(digest,prompt_hash,stage,mode,action,reason,"
        "profile_id,title,content_hash,policy_id,evidence) "
        "SELECT sha256(convert_to('fixture-'||d.id,'UTF8')),prompt_hash,stage,mode,action,reason,"
        "profile_id,title,content_hash,policy_id,evidence FROM review_gate_decisions d "
        f"WHERE d.body_id IS NULL AND {selected}",
        scope,
    )
    db.execute(
        "UPDATE review_gate_decisions d SET url_id=u.id,body_id=b.id,"
        + ",".join(f"{column}=NULL" for column in INLINE)
        + ",policy=NULL,policy_id=NULL FROM review_gate_urls u,review_gate_decision_bodies b "
        "WHERE u.url=d.url AND b.digest=sha256(convert_to('fixture-'||d.id,'UTF8')) "
        f"AND {selected}",
        scope,
    )


def inline(ids=None):
    """Rewrite admitted rows into the legacy all-inline shape production holds."""
    db.execute(
        "UPDATE review_gate_decisions d SET url=u.url,"
        + ",".join(f"{column}=b.{column}" for column in INLINE if column != "url")
        + ",policy_id=b.policy_id,url_id=NULL,body_id=NULL "
        "FROM review_gate_urls u,review_gate_decision_bodies b "
        "WHERE u.id=d.url_id AND b.id=d.body_id AND (%(all)s OR d.id=ANY(%(ids)s::bigint[]))",
        {"ids": ids, "all": ids is None},
    )


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
    inline()
    db.execute(
        "UPDATE review_gate_decisions SET evidence=evidence||"
        '\'{"routing":{"outcome":"reject","would_review":false}}\'::jsonb '
        "WHERE url='https://example.test/eng'"
    )
    decisions = db.query("SELECT id,stage FROM review_gate_decisions ORDER BY id")
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


def test_reference_only_rows_read_exactly_like_inline_rows(
    f, client, admin_headers, user_headers, owned_job
):
    me, job_id = owned_job
    _, tasks, jobs, contents = admitted(f)
    db.execute("UPDATE review_gate_decisions SET user_id=%s", (me,))
    before = observed(client, admin_headers, user_headers, tasks, jobs, contents, job_id)
    assert before["all"]["total"] == 4
    assert before["url"]["total"] == 2
    assert before["stage"]["total"] == 2
    assert before["personal"]["total"] == 2
    assert before["exclusions"] == {"https://example.test/nurse"}
    assert before["comparisons"]["routing"] and before["comparisons"]["review_gate"]
    reference_only()
    assert (
        db.query_one(
            "SELECT count(*) n FROM review_gate_decisions WHERE url IS NOT NULL OR stage IS NOT NULL "
            "OR evidence IS NOT NULL OR policy IS NOT NULL OR policy_id IS NOT NULL"
        )["n"]
        == 0
    )
    assert observed(client, admin_headers, user_headers, tasks, jobs, contents, job_id) == before


def test_one_task_may_mix_shapes_mid_migration(f, client, admin_headers, user_headers, owned_job):
    me, job_id = owned_job
    _, tasks, jobs, contents = admitted(f)
    db.execute("UPDATE review_gate_decisions SET user_id=%s", (me,))
    before = observed(client, admin_headers, user_headers, tasks, jobs, contents, job_id)
    first = db.query_one("SELECT min(id) AS id FROM review_gate_decisions")["id"]
    reference_only([first])
    assert observed(client, admin_headers, user_headers, tasks, jobs, contents, job_id) == before


def test_copied_rows_that_disagree_with_their_body_fail_explicitly(f, client, admin_headers):
    _, tasks, _, _ = admitted(f)
    row = db.query_one("SELECT * FROM review_gate_decisions WHERE task_id=%s LIMIT 1", (tasks[0],))
    db.execute("INSERT INTO review_gate_urls(url) VALUES(%s) ON CONFLICT DO NOTHING", (row["url"],))
    body = db.query_one(
        "INSERT INTO review_gate_decision_bodies(digest,prompt_hash,stage,mode,action,reason,"
        "profile_id,title,content_hash,policy_id,evidence) "
        "SELECT 'x'::bytea,prompt_hash,stage,mode,action,reason,profile_id,'Different title',"
        "content_hash,policy_id,evidence FROM review_gate_decisions WHERE id=%s RETURNING id",
        (row["id"],),
    )
    db.execute(
        "UPDATE review_gate_decisions SET body_id=%s,"
        "url_id=(SELECT id FROM review_gate_urls WHERE url=%s) WHERE id=%s",
        (body["id"], row["url"], row["id"]),
    )
    from api.review_policy_storage import PolicySnapshotUnavailable

    with pytest.raises(PolicySnapshotUnavailable, match="disagree"):
        review_gate_records.existing(tasks[0])
    with pytest.raises(PolicySnapshotUnavailable, match="disagree"):
        client.get("/v1/admin/review-gates/decisions", headers=admin_headers)


def test_a_row_must_keep_one_complete_shape(f):
    _, tasks, _, _ = admitted(f)
    from psycopg.errors import CheckViolation

    with pytest.raises(CheckViolation):
        db.execute("UPDATE review_gate_decisions SET stage=NULL WHERE task_id=%s", (tasks[0],))
    with pytest.raises(CheckViolation):
        db.execute("UPDATE review_gate_decisions SET url=NULL WHERE task_id=%s", (tasks[0],))


def test_task_url_uniqueness_holds_for_reference_only_rows(f):
    _, tasks, _, _ = admitted(f)
    reference_only()
    from psycopg.errors import UniqueViolation

    with pytest.raises(UniqueViolation):
        db.execute(
            "INSERT INTO review_gate_decisions(task_id,url_id,body_id) "
            "SELECT task_id,url_id,body_id FROM review_gate_decisions WHERE task_id=%s LIMIT 1",
            (tasks[0],),
        )
