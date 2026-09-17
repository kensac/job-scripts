import pytest

from api import db, review_gate, review_gate_records
from api.ai import verdicts
from tests.test_review_gate import configure, proven_job


def test_admissions_survive_retention_and_retries_do_not_rewrite_facts(f):
    configure()
    job_id = f.make_job(title="Registered Nurse")
    job = db.query_one("SELECT id,url,title,company FROM jobs WHERE id=%s", (job_id,))
    task = f.make_task("run_filter_batch_chunk", {"user_id": f.make_user(), "filter_id": 42})
    _, decisions = review_gate.partition(
        task, "test-hash", [job], {}, model="test-model", transport="batch"
    )
    row = db.query_one(
        "SELECT * FROM review_gate_decisions WHERE id=%s", (decisions[job["url"]]["decision_id"],)
    )
    assert row["action"] == "skip"
    assert row["job_id"] == job_id
    assert row["filter_id"] == 42
    assert row["evidence"]["planned_model"] == "test-model"
    configure(title="off")
    assert review_gate.partition(task, "test-hash", [job], {})[0] == []
    assert db.query_one("SELECT count(*) n FROM review_gate_decisions")["n"] == 1
    db.execute("DELETE FROM tasks WHERE id=%s", (task,))
    db.execute("DELETE FROM jobs WHERE id=%s", (job_id,))
    assert db.query_one("SELECT * FROM review_gate_decisions WHERE id=%s", (row["id"],)) == row


def test_profile_snapshot_keeps_evidence_after_source_task_retention(f):
    configure(title="off", shared="enforce")
    job, profile_task = proven_job(f)
    task = f.make_task("run_filter_batch_chunk")
    review_gate.partition(task, "test-hash", [job], {job["url"]: "exact posting content"})
    row = db.query_one("SELECT * FROM review_gate_decisions WHERE task_id=%s", (task,))
    assert row["stage"] == "profile"
    assert row["evidence"]["profile"]["primary_role_family"] == "legal"
    assert row["evidence"]["profile_input_content"] == "exact posting content"
    assert row["content_hash"] == review_gate_records.content_hash("exact posting content")
    db.execute("DELETE FROM tasks WHERE id=%s", (profile_task,))
    assert (
        review_gate.partition(task, "test-hash", [job], {job["url"]: "exact posting content"})[0]
        == []
    )


def test_changed_input_refuses_reuse_in_same_run(f):
    task = f.make_task("run_filter_batch_chunk")
    job = {"url": "https://example.test/a", "title": "Engineer", "company": "Example"}
    review_gate.partition(task, "test-hash", [job], {job["url"]: "first"})
    with pytest.raises(RuntimeError, match="immutable run"):
        review_gate.partition(task, "test-hash", [job], {job["url"]: "second"})
    assert db.query_one("SELECT count(*) n FROM review_gate_decisions")["n"] == 1


def test_locked_writer_rechecks_input_after_another_admission_wins(f, monkeypatch):
    task = f.make_task("run_filter_batch_chunk")
    job = {"url": "https://example.test/a", "title": "Engineer", "company": "Example"}
    review_gate.partition(task, "test-hash", [job], {job["url"]: "first"})
    original = review_gate_records.existing
    reads = 0

    def stale_first_read(task_id):
        nonlocal reads
        reads += 1
        return {} if reads == 1 else original(task_id)

    monkeypatch.setattr(review_gate_records, "existing", stale_first_read)
    with pytest.raises(RuntimeError, match="immutable run"):
        review_gate.partition(task, "test-hash", [job], {job["url"]: "second"})
    assert reads >= 2


def test_managed_sponsor_is_frozen_without_guessing_current_ownership(f):
    sponsor = f.make_user()
    task = f.make_task(
        "run_managed_board_batch",
        {"sponsor_user_id": sponsor, "managed_board_id": 31, "revision": 7},
    )
    job = {"url": "https://example.test/a", "title": "Engineer", "company": "Example"}
    review_gate.partition(task, "test-hash", [job], {})
    assert db.query_one("SELECT user_id,managed_board_id,revision FROM review_gate_decisions") == {
        "user_id": sponsor,
        "managed_board_id": 31,
        "revision": 7,
    }


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
    ],
)
def test_paid_outcome_uses_exact_stored_price_and_retains_unknown(f, usage):
    task = f.make_task("run_filter_batch_chunk")
    job = {"url": "https://example.test/a", "title": "Engineer", "company": "Example"}
    _, decisions = review_gate.partition(task, "test-hash", [job], {})
    decision_id = decisions[job["url"]]["decision_id"]
    query_id = verdicts.record_ai_verdict(
        url=job["url"],
        check_type="custom",
        rejected=False,
        reason=None,
        parsed_json="{}",
        usage=usage,
        model="gpt-5-nano",
        batch_id="batch-fact",
        batched=True,
        on_record=lambda query_id: review_gate_records.record_outcome(decision_id, query_id),
    )
    review_gate_records.record_outcome(decision_id, query_id)
    row = db.query_one("SELECT * FROM review_gate_outcomes")
    assert row["query_id"] == query_id
    assert row["rejected"] is False
    assert (
        row["recorded_cost_usd"]
        == db.query_one("SELECT cost_usd FROM ai_queries WHERE id=%s", (query_id,))["cost_usd"]
    )
    if not usage:
        assert row["recorded_cost_usd"] is None
    assert db.query_one("SELECT count(*) n FROM review_gate_outcomes")["n"] == 1
    db.execute("DELETE FROM tasks WHERE id=%s", (task,))
    db.execute("DELETE FROM ai_queries WHERE id=%s", (query_id,))
    assert db.query_one("SELECT * FROM review_gate_outcomes") == row


def test_outcome_failure_rolls_back_verdict_write(f):
    def fail(_query_id):
        raise RuntimeError("incomplete evidence")

    with pytest.raises(RuntimeError, match="incomplete evidence"):
        verdicts.record_ai_verdict(
            url="https://example.test/a",
            check_type="custom",
            rejected=False,
            reason=None,
            parsed_json="{}",
            usage={},
            model=None,
            on_record=fail,
        )
    assert db.query_one("SELECT count(*) n FROM ai_queries WHERE check_type='custom'")["n"] == 0
