"""Admission writes a per-task row that references shared content."""

import pytest

from api import db, queue, review_decision_storage, review_gate, review_gate_records
from api.review_policy_storage import PolicySnapshotUnavailable
from tests.test_review_gate import configure

JOBS = [
    {"url": "https://example.test/nurse", "title": "Registered Nurse", "company": "Care"},
    {"url": "https://example.test/eng", "title": "Software Engineer", "company": "Example"},
]
CONTENTS = {job["url"]: f"content {job['title']}" for job in JOBS}


def test_admission_writes_only_references_and_shares_one_body(f):
    configure()
    tasks = [f.make_task("run_filter_batch_chunk", {"filter_id": 5}) for _ in range(3)]
    reads = [
        review_gate.partition(task, "test-hash", JOBS, CONTENTS, model="m", transport="batch")[1]
        for task in tasks
    ]
    stored = db.query("SELECT * FROM review_gate_decisions ORDER BY id")
    assert len(stored) == 6
    for row in stored:
        assert row["url_id"] is not None and row["body_id"] is not None
        assert row["filter_id"] == 5
        assert set(row) == {
            "id",
            "task_id",
            "url_id",
            "body_id",
            "job_id",
            "user_id",
            "filter_id",
            "managed_board_id",
            "revision",
            "created_at",
        }
    # Three tasks, two postings: two bodies and two URLs, not six of each.
    assert db.query_one("SELECT count(*) n FROM review_gate_decision_bodies")["n"] == 2
    assert db.query_one("SELECT count(*) n FROM review_gate_urls")["n"] == 2
    assert [{url: d["skip"] for url, d in read.items()} for read in reads] == [
        {"https://example.test/nurse": True, "https://example.test/eng": False}
    ] * 3
    nurse = review_gate_records.existing(tasks[0])["https://example.test/nurse"]
    assert nurse["stage"] == "title" and nurse["action"] == "skip"
    assert nurse["title"] == "Registered Nurse"
    assert nurse["evidence"]["company"] == "Care"
    assert nurse["evidence"]["planned_model"] == "m"
    assert nurse["content_hash"] == review_gate_records.content_hash("content Registered Nurse")
    assert nurse["policy"]["title_mode"] == "enforce"


def test_concurrent_admissions_intern_one_body(f):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    job = {"url": "https://example.test/a", "title": "Engineer", "company": "Example"}
    tasks = [f.make_task("run_filter_batch_chunk") for _ in range(4)]
    barrier = Barrier(4)

    def admit(task):
        barrier.wait(timeout=10)
        return review_gate.partition(task, "test-hash", [job], {})

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(admit, tasks))
    assert db.query_one("SELECT count(*) n FROM review_gate_decisions")["n"] == 4
    assert db.query_one("SELECT count(*) n FROM review_gate_decision_bodies")["n"] == 1
    assert db.query_one("SELECT count(*) n FROM review_gate_urls")["n"] == 1


def test_body_digest_collision_never_substitutes_another_body(f, monkeypatch):
    job = {"url": "https://example.test/a", "title": "Engineer", "company": "Example"}
    review_gate.partition(f.make_task("run_filter_batch_chunk"), "test-hash", [job], {})
    # Every body now hashes to the stored one: a collision by construction.
    monkeypatch.setattr(
        review_decision_storage,
        "body_digest",
        lambda alias: "(SELECT digest FROM review_gate_decision_bodies LIMIT 1)",
    )
    other = f.make_task("run_filter_batch_chunk")
    with pytest.raises(PolicySnapshotUnavailable, match="exact body"):
        review_gate.partition(other, "test-hash", [{**job, "title": "Different"}], {})
    assert (
        db.query_one("SELECT count(*) n FROM review_gate_decisions WHERE task_id=%s", (other,))["n"]
        == 0
    )


def test_one_reference_row_per_job_commits_with_the_plan_or_not_at_all(f, monkeypatch):
    # exclusions() derives the plan's skips from these rows (#752), so they
    # must exist for every job exactly when the plan does.
    configure()
    task = f.make_task("run_filter_batch_chunk")
    original = queue.merge_payload

    def plan_write_fails(task_id, data, drop=()):
        if "review_gate" in data:
            raise RuntimeError("plan write failed")
        return original(task_id, data, drop)

    monkeypatch.setattr(queue, "merge_payload", plan_write_fails)
    with pytest.raises(RuntimeError, match="plan write failed"):
        review_gate.partition(task, "test-hash", JOBS, CONTENTS)
    for table in ("review_gate_decisions", "review_gate_decision_bodies", "review_gate_urls"):
        assert db.query_one(f"SELECT count(*) n FROM {table}")["n"] == 0
    monkeypatch.setattr(queue, "merge_payload", original)
    kept, decisions = review_gate.partition(task, "test-hash", JOBS, CONTENTS)
    stored = db.query_one(
        "SELECT count(*) n FROM review_gate_decisions WHERE task_id=%s AND body_id IS NOT NULL",
        (task,),
    )
    assert stored["n"] == len(JOBS)
    skipped = {url for url, decision in decisions.items() if decision["skip"]}
    assert skipped == {"https://example.test/nurse"}
    assert review_gate_records.exclusions(task, "test-hash") == skipped
    assert [job["url"] for job in kept] == ["https://example.test/eng"]
