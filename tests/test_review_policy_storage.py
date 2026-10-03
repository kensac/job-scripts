from api import db, review_gate, review_gate_records


def admission(f, *, title="Engineer"):
    task = f.make_task("run_filter_batch_chunk")
    job = {"url": f"https://example.test/{task}", "title": title, "company": "Example"}
    review_gate.partition(task, "policy-test", [job], {})
    row = db.query_one("SELECT * FROM review_gate_decisions WHERE task_id=%s", (task,))
    assert row is not None
    return task, job, row


def test_admission_interns_policy_without_changing_snapshot(f):
    task, _, row = admission(f)
    assert row.get("policy_id") is not None
    snapshot = db.query_one(
        "SELECT policy FROM review_gate_policies WHERE id=%s", (row["policy_id"],)
    )
    assert snapshot == {"policy": row["policy"]}
    assert review_gate_records.existing(task)[row["url"]]["policy"] == row["policy"]


def test_separate_runs_share_exact_policy_snapshot(f):
    _, _, first = admission(f)
    _, _, second = admission(f)
    assert first.get("policy_id") is not None
    assert first["policy_id"] == second["policy_id"]
    assert db.query_one("SELECT count(*) AS n FROM review_gate_policies")["n"] == 1
