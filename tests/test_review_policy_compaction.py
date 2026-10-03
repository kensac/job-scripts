from api import db, review_gate_records
from tests.test_review_policy_storage import admission


def test_new_admission_keeps_only_the_shared_policy(f):
    task, _, row = admission(f)
    assert row["policy_id"] is not None
    assert row["policy"] is None
    historical = review_gate_records.existing(task)[row["url"]]
    assert historical["policy"] == {
        "title_mode": "off",
        "profile_mode": "off",
        "scopes": {},
        "lookup_timeout_ms": 1000,
    }


def test_schema_accepts_reference_only_policy_without_losing_history(f):
    task, _, row = admission(f)
    before = review_gate_records.existing(task)
    db.execute("UPDATE review_gate_decisions SET policy=NULL WHERE id=%s", (row["id"],))
    assert review_gate_records.existing(task) == before
