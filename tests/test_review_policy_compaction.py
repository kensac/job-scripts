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


def legacy_decision(f, policy):
    task, job, row = admission(f)
    db.execute(
        "UPDATE review_gate_decisions SET policy=%s::jsonb,policy_id=NULL WHERE id=%s",
        (policy, row["id"]),
    )
    return task, job, row


def run(mode, through, **kwargs):
    from api.review_policy_storage import migrate_chunk

    return migrate_chunk(after=0, through=through, limit=10, mode=mode, **kwargs)


def test_compaction_and_restore_are_exact_restartable_and_preserve_all_other_facts(f):
    from tests.test_review_gate_reads import outcome

    exact = '{"precision":0.123456789012345678901234567890,"unicode":"é","null":null,"list":[2,1]}'
    _, _, first = legacy_decision(f, exact)
    _, _, last = legacy_decision(f, exact)
    outcome(first["id"], 77, None)
    outcome(first["id"], 78, 0.1)
    assert run("copy", last["id"])["copied"] == 2
    before = db.query("SELECT to_jsonb(d)-'policy' AS row FROM review_gate_decisions d ORDER BY id")
    policies_before = db.query(
        "SELECT id,policy::text AS policy FROM review_gate_decisions ORDER BY id"
    )
    outcomes_before = db.query("SELECT * FROM review_gate_outcomes ORDER BY id")
    dictionary_before = db.query(
        "SELECT id,digest,policy::text AS policy FROM review_gate_policies ORDER BY id"
    )
    size_before = db.query_one(
        "SELECT sum(pg_column_size(policy)) AS n FROM review_gate_decisions"
    )["n"]
    compacted = run("compact", last["id"], backup_complete=True, compatible_readers=True)
    assert compacted["compacted"] == 2 and compacted["inline_remaining"] == 0
    assert compacted["inline_policy_bytes_removed"] == size_before
    assert (
        db.query_one("SELECT count(*) n FROM review_gate_decisions WHERE policy IS NOT NULL")["n"]
        == 0
    )
    replay = run("compact", last["id"], backup_complete=True, compatible_readers=True)
    assert replay["compacted"] == replay["inline_policy_bytes_removed"] == 0
    verified = run("verify", last["id"])
    assert verified["verified"] == 2 and verified["inline_remaining"] == 0
    assert run("copy", last["id"])["copied"] == 0
    restored = run("restore", last["id"])
    assert restored["restored"] == restored["inline_remaining"] == 2
    assert run("restore", last["id"])["restored"] == 0
    assert (
        db.query("SELECT id,policy::text AS policy FROM review_gate_decisions ORDER BY id")
        == policies_before
    )
    assert (
        db.query("SELECT to_jsonb(d)-'policy' AS row FROM review_gate_decisions d ORDER BY id")
        == before
    )
    assert db.query("SELECT * FROM review_gate_outcomes ORDER BY id") == outcomes_before
    assert (
        db.query("SELECT id,digest,policy::text AS policy FROM review_gate_policies ORDER BY id")
        == dictionary_before
    )


def test_compaction_requires_both_explicit_operating_gates(f):
    import pytest

    _, _, row = admission(f)
    for gates in ({}, {"backup_complete": True}, {"compatible_readers": True}):
        with pytest.raises(ValueError, match="completed backup and compatible readers"):
            run("compact", row["id"], **gates)


def test_missing_mismatched_and_invalid_digest_refs_never_clear_or_restore(f):
    import pytest

    from api.review_policy_storage import PolicySnapshotUnavailable

    _, _, row = legacy_decision(f, '{"legacy":true}')
    with pytest.raises(PolicySnapshotUnavailable, match="verified policy reference"):
        run("compact", row["id"], backup_complete=True, compatible_readers=True)
    run("copy", row["id"])
    db.execute(
        "UPDATE review_gate_decisions SET policy='{\"different\":true}' WHERE id=%s", (row["id"],)
    )
    before = db.query("SELECT * FROM review_gate_decisions")
    for mode in ("compact", "restore"):
        with pytest.raises(PolicySnapshotUnavailable, match="verification failed"):
            run(mode, row["id"], backup_complete=True, compatible_readers=True)
        assert db.query("SELECT * FROM review_gate_decisions") == before
    db.execute("UPDATE review_gate_decisions SET policy=NULL")
    db.execute(
        "UPDATE review_gate_policies SET digest='invalid'::bytea WHERE id=(SELECT policy_id FROM review_gate_decisions WHERE id=%s)",
        (row["id"],),
    )
    before = db.query("SELECT * FROM review_gate_decisions")
    for mode in ("verify", "compact", "restore"):
        with pytest.raises(PolicySnapshotUnavailable, match="verification failed"):
            run(mode, row["id"], backup_complete=True, compatible_readers=True)
        assert db.query("SELECT * FROM review_gate_decisions") == before


def test_compaction_is_bounded_and_stops_atomically_at_an_invalid_chunk(f):
    import pytest

    from api.review_policy_storage import PolicySnapshotUnavailable, migrate_chunk

    _, _, first = legacy_decision(f, '{"one":1}')
    _, _, last = legacy_decision(f, '{"two":2}')
    run("copy", last["id"])
    db.execute("UPDATE review_gate_decisions SET policy='{\"wrong\":1}' WHERE id=%s", (last["id"],))
    before = db.query("SELECT * FROM review_gate_decisions ORDER BY id")
    with pytest.raises(PolicySnapshotUnavailable):
        run("compact", last["id"], backup_complete=True, compatible_readers=True)
    assert db.query("SELECT * FROM review_gate_decisions ORDER BY id") == before
    result = migrate_chunk(
        after=0,
        through=last["id"],
        limit=1,
        mode="compact",
        backup_complete=True,
        compatible_readers=True,
    )
    assert result["after"] == first["id"] and result["compacted"] == 1
    assert db.query_one("SELECT policy FROM review_gate_decisions WHERE id=%s", (last["id"],))[
        "policy"
    ] == {"wrong": 1}


def test_restore_does_not_assign_unknown_legacy_provenance(f):
    _, _, row = legacy_decision(f, '{"historical":true}')
    before = db.query("SELECT * FROM review_gate_decisions")
    result = run("restore", row["id"])
    assert result["restored"] == 0 and result["unreferenced"] == result["inline_remaining"] == 1
    assert db.query("SELECT * FROM review_gate_decisions") == before


def test_concurrent_compaction_counts_each_inline_policy_once(f):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, _, row = legacy_decision(f, '{"policy":true}')
    run("copy", row["id"])
    barrier = Barrier(2)

    def compact():
        barrier.wait(timeout=10)
        return run("compact", row["id"], backup_complete=True, compatible_readers=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: compact(), range(2)))
    assert sorted(result["compacted"] for result in results) == [0, 1]
    assert run("verify", row["id"])["verified"] == 1


def test_dictionary_lock_timeout_preserves_inline_policy_and_allows_retry(f):
    from concurrent.futures import ThreadPoolExecutor

    import pytest
    from psycopg.errors import LockNotAvailable

    from core.pool import connection

    _, _, row = legacy_decision(f, '{"policy":true}')
    run("copy", row["id"])
    before = db.query("SELECT * FROM review_gate_decisions")

    def compact():
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='50ms'")
            return run("compact", row["id"], backup_complete=True, compatible_readers=True)

    with connection() as conn, conn.transaction():
        conn.execute(
            "SELECT id FROM review_gate_policies WHERE id=(SELECT policy_id FROM review_gate_decisions WHERE id=%s) FOR UPDATE",
            (row["id"],),
        )
        with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(LockNotAvailable):
            executor.submit(compact).result(timeout=10)
    assert db.query("SELECT * FROM review_gate_decisions") == before
    assert (
        run("compact", row["id"], backup_complete=True, compatible_readers=True)["compacted"] == 1
    )


def test_failure_after_clear_rolls_back_the_chunk(f, monkeypatch):
    import pytest

    _, _, row = legacy_decision(f, '{"policy":true}')
    run("copy", row["id"])
    before = db.query("SELECT * FROM review_gate_decisions")
    original = db.execute_count

    def fail_after_write(sql, params=None):
        original(sql, params)
        raise RuntimeError("transaction interrupted")

    monkeypatch.setattr(db, "execute_count", fail_after_write)
    with pytest.raises(RuntimeError, match="transaction interrupted"):
        run("compact", row["id"], backup_complete=True, compatible_readers=True)
    assert db.query("SELECT * FROM review_gate_decisions") == before
