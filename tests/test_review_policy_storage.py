from api import db, review_gate, review_gate_records
from tests.test_review_decision_storage import inline


def admission(f, *, title="Engineer"):
    task = f.make_task("run_filter_batch_chunk")
    job = {"url": f"https://example.test/{task}", "title": title, "company": "Example"}
    review_gate.partition(task, "policy-test", [job], {})
    from api.review_decision_storage import DECISIONS

    # The resolved row: effective references, and the inline policy column.
    row = db.query_one(
        f"SELECT d.*,d.inline_policy AS policy FROM {DECISIONS} WHERE d.task_id=%s", (task,)
    )
    assert row is not None
    return task, job, row


def test_admission_interns_policy_without_changing_snapshot(f):
    task, _, row = admission(f)
    assert row.get("policy_id") is not None
    snapshot = db.query_one(
        "SELECT policy FROM review_gate_policies WHERE id=%s", (row["policy_id"],)
    )
    assert snapshot is not None
    assert row["policy"] is None
    assert review_gate_records.existing(task)[row["url"]]["policy"] == snapshot["policy"]


def test_separate_runs_share_exact_policy_snapshot(f):
    _, _, first = admission(f)
    _, _, second = admission(f)
    assert first.get("policy_id") is not None
    assert first["policy_id"] == second["policy_id"]
    assert db.query_one("SELECT count(*) AS n FROM review_gate_policies")["n"] == 1


def test_policy_change_gets_a_new_snapshot_and_partial_retry_keeps_old_policy(f):
    from api import review_policy_storage

    task, job, first = admission(f)
    old = review_gate_records.existing(task)[job["url"]]["policy"]
    changed = {**old, "lookup_timeout_ms": old["lookup_timeout_ms"] + 1}
    db.execute(
        "UPDATE app_config SET value=%s WHERE key='filter_review_gate'", (db.jsonb(changed),)
    )
    added = {**job, "url": job["url"] + "/next"}
    review_gate.partition(task, "policy-test", [job, added], {})
    new_task, _, new = admission(f)
    historical = review_gate_records.existing(task)
    assert historical[added["url"]]["policy"] == old
    assert new["policy_id"] != first["policy_id"]
    assert review_gate_records.existing(new_task)[new["url"]]["policy"] == changed
    assert review_policy_storage.intern(db.jsonb(old)) == first["policy_id"]


def test_concurrent_policy_interning_has_one_exact_snapshot(f):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from api import review_policy_storage

    barrier = Barrier(4)

    def write():
        barrier.wait(timeout=10)
        return review_policy_storage.intern('{"scopes":{},"precise":0.123456789012345678901}')

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: write(), range(4)))
    assert len(set(ids)) == 1
    assert db.query_one("SELECT count(*) n FROM review_gate_policies")["n"] == 1


def test_digest_collision_never_substitutes_or_overwrites_a_policy(f):
    import pytest

    from api import review_policy_storage

    wanted = '{"scopes":{"one":{"title_recipe":true}}}'
    db.execute(
        "INSERT INTO review_gate_policies(digest,policy) "
        "VALUES(sha256(convert_to((%s::jsonb)::text,'UTF8')),'{\"different\":true}')",
        (wanted,),
    )
    before = db.query("SELECT * FROM review_gate_policies")
    with pytest.raises(review_policy_storage.PolicySnapshotUnavailable, match="exact snapshot"):
        review_policy_storage.intern(wanted)
    assert db.query("SELECT * FROM review_gate_policies") == before


def test_admission_failure_rolls_back_policy_and_decisions(f, monkeypatch):
    import pytest

    from api import review_policy_storage

    original = review_policy_storage.intern

    def fail_after_intern(policy):
        original(policy)
        raise RuntimeError("failed admission")

    monkeypatch.setattr(review_policy_storage, "intern", fail_after_intern)
    with pytest.raises(RuntimeError, match="failed admission"):
        admission(f)
    assert db.query_one("SELECT count(*) n FROM review_gate_decisions")["n"] == 0
    assert db.query_one("SELECT count(*) n FROM review_gate_policies")["n"] == 0


def test_missing_reference_fails_even_when_inline_policy_exists(f):
    import pytest

    from api import review_policy_storage

    task, _, row = admission(f)
    inline([row["id"]])
    db.execute(
        "UPDATE review_gate_decisions d SET policy=p.policy "
        "FROM review_gate_policies p WHERE d.id=%s AND p.id=d.policy_id",
        (row["id"],),
    )
    with db.transaction():
        db.execute("SET LOCAL session_replication_role=replica")
        db.execute(
            "UPDATE review_gate_decisions SET policy_id=%s WHERE id=%s",
            (row["policy_id"] + 999, row["id"]),
        )
    with pytest.raises(review_policy_storage.PolicySnapshotUnavailable, match="unavailable"):
        review_gate_records.existing(task)


def test_reference_only_and_legacy_rows_preserve_admin_and_personal_shapes(
    f, client, admin_headers
):
    from api import pagination, review_gate_reads

    task, _, row = admission(f)
    baseline = client.get("/v1/admin/review-gates/decisions", headers=admin_headers).json()
    assert row["policy"] is None
    assert review_gate_records.existing(task)[row["url"]]["policy"] == baseline["rows"][0]["policy"]
    personal = review_gate_reads.read_decisions(
        "TRUE", {}, pagination.Page.from_params(1, 25, maximum=100), {}, personal=True
    )
    assert personal.rows[0].policy == {} and personal.rows[0].evidence == {}
    inline([row["id"]])
    assert client.get("/v1/admin/review-gates/decisions", headers=admin_headers).json() == baseline
    db.execute(
        "UPDATE review_gate_decisions d SET policy=p.policy "
        "FROM review_gate_policies p WHERE d.id=%s AND p.id=d.policy_id",
        (row["id"],),
    )
    assert client.get("/v1/admin/review-gates/decisions", headers=admin_headers).json() == baseline
    db.execute("UPDATE review_gate_decisions SET policy_id=NULL WHERE id=%s", (row["id"],))
    assert client.get("/v1/admin/review-gates/decisions", headers=admin_headers).json() == baseline
