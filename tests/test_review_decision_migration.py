"""The historical backfill: one rewrite per row, verified before anything is cleared."""

import pytest

from api import db, review_gate_records
from api.review_policy_storage import PolicySnapshotUnavailable
from tests.test_review_decision_storage import INLINE
from tests.test_review_gate_reads import outcome
from tests.test_review_policy_storage import admission

REFERENCES = "-'url_id'-'body_id'-'policy_id'"


def legacy(f, policy='{"legacy":true}'):
    """The shape 7.4M production rows hold: every column inline, no references."""
    task, job, row = admission(f)
    db.execute(
        "UPDATE review_gate_decisions SET policy=%s::jsonb,policy_id=NULL WHERE id=%s",
        (policy, row["id"]),
    )
    return task, job, row


def run(mode, through, **kwargs):
    from api.review_decision_storage import migrate_chunk

    return migrate_chunk(
        after=kwargs.pop("after", 0), through=through, limit=10, mode=mode, **kwargs
    )


def compact(through, **kwargs):
    return run("compact", through, backup_complete=True, compatible_readers=True, **kwargs)


def rows():
    return db.query("SELECT * FROM review_gate_decisions ORDER BY id")


def test_copy_references_policy_url_and_body_in_one_pass_without_changing_values(f):
    exact = '{"precision":0.123456789012345678901234567890,"unicode":"é","null":null,"list":[2,1]}'
    first_task, _, first = legacy(f, exact)
    second_task, _, last = legacy(f, exact)
    outcome(first["id"], 77, None)
    before = db.query(
        f"SELECT to_jsonb(d){REFERENCES} AS row FROM review_gate_decisions d ORDER BY id"
    )
    outcomes = db.query("SELECT * FROM review_gate_outcomes ORDER BY id")
    reads = {task: review_gate_records.existing(task) for task in (first_task, second_task)}
    result = run("copy", last["id"])
    assert result["copied"] == 2 and result["verified"] == 2 and result["unreferenced"] == 0
    assert result["after"] == last["id"]
    after = db.query("SELECT url_id,body_id,policy_id FROM review_gate_decisions ORDER BY id")
    assert all(None not in row.values() for row in after)
    # Both admissions made the same decision about different postings.
    assert len({row["body_id"] for row in after}) == 1
    assert len({row["url_id"] for row in after}) == 2
    assert (
        db.query_one(
            "SELECT p.policy::text AS policy FROM review_gate_policies p WHERE id=%s",
            (after[0]["policy_id"],),
        )["policy"]
        == db.query_one("SELECT %s::jsonb::text AS policy", (exact,))["policy"]
    )
    assert (
        db.query(f"SELECT to_jsonb(d){REFERENCES} AS row FROM review_gate_decisions d ORDER BY id")
        == before
    )
    assert db.query("SELECT * FROM review_gate_outcomes ORDER BY id") == outcomes
    assert {task: review_gate_records.existing(task) for task in reads} == reads
    replay = run("copy", last["id"])
    assert replay["copied"] == 0 and replay["verified"] == 2


def test_compaction_and_restore_are_exact_and_restartable(f):
    first_task, _, _ = legacy(f, '{"precision":0.10000000000000000000001}')
    _, _, last = legacy(f, '{"other":[1,2]}')
    run("copy", last["id"])
    copied = rows()
    reads = review_gate_records.existing(first_task)
    inline_bytes = db.query_one(
        "SELECT sum("
        + "+".join(f"COALESCE(pg_column_size({c}),0)" for c in (*INLINE, "policy", "policy_id"))
        + ") AS n FROM review_gate_decisions"
    )["n"]
    result = compact(last["id"])
    assert result["compacted"] == 2 and result["inline_remaining"] == 0
    assert result["inline_bytes_removed"] == inline_bytes
    cleared = " AND ".join(f"{c} IS NULL" for c in (*INLINE, "policy", "policy_id"))
    assert db.query_one(f"SELECT count(*) n FROM review_gate_decisions WHERE {cleared}")["n"] == 2
    assert review_gate_records.existing(first_task) == reads
    replay = compact(last["id"])
    assert replay["compacted"] == replay["inline_bytes_removed"] == 0
    verified = run("verify", last["id"])
    assert verified["verified"] == 2 and verified["inline_remaining"] == 0
    assert run("copy", last["id"])["copied"] == 0
    restored = run("restore", last["id"])
    assert restored["restored"] == restored["inline_remaining"] == 2
    assert run("restore", last["id"])["restored"] == 0
    assert rows() == copied
    assert review_gate_records.existing(first_task) == reads


def test_reference_only_policy_rows_get_their_exact_snapshot_back_inline(f):
    task, _, row = admission(f)
    assert row["policy"] is None and row["policy_id"] is not None
    reads = review_gate_records.existing(task)
    run("copy", row["id"])
    compact(row["id"])
    run("restore", row["id"])
    restored = db.query_one("SELECT * FROM review_gate_decisions WHERE id=%s", (row["id"],))
    snapshot = db.query_one(
        "SELECT policy FROM review_gate_policies WHERE id=%s", (row["policy_id"],)
    )
    assert restored["policy"] == snapshot["policy"]
    assert {k: v for k, v in restored.items() if k not in ("policy", "url_id", "body_id")} == {
        k: v for k, v in row.items() if k not in ("policy", "url_id", "body_id")
    }
    assert review_gate_records.existing(task) == reads


def test_compaction_requires_both_explicit_operating_gates(f):
    _, _, row = admission(f)
    for gates in ({}, {"backup_complete": True}, {"compatible_readers": True}):
        with pytest.raises(ValueError, match="completed backup and compatible readers"):
            run("compact", row["id"], **gates)


def test_unreferenced_rows_are_counted_never_compacted_or_given_provenance(f):
    _, _, row = legacy(f)
    before = rows()
    verified = run("verify", row["id"])
    assert verified["unreferenced"] == verified["inline_remaining"] == 1
    with pytest.raises(PolicySnapshotUnavailable, match="verified decision reference"):
        compact(row["id"])
    restored = run("restore", row["id"])
    assert restored["restored"] == 0 and restored["unreferenced"] == 1
    assert rows() == before


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE review_gate_decisions SET title='Changed'",
        "UPDATE review_gate_decisions SET policy='{\"changed\":true}'",
        "UPDATE review_gate_decision_bodies SET digest='invalid'::bytea",
        "UPDATE review_gate_policies SET digest='invalid'::bytea "
        "WHERE id=(SELECT policy_id FROM review_gate_decision_bodies)",
    ],
)
def test_disagreeing_or_corrupt_references_never_clear_or_restore(f, tamper):
    _, _, row = legacy(f)
    run("copy", row["id"])
    db.execute(tamper)
    before = rows()
    for mode in ("verify", "compact", "restore"):
        with pytest.raises(PolicySnapshotUnavailable, match="verification failed"):
            run(mode, row["id"], backup_complete=True, compatible_readers=True)
        assert rows() == before


def test_copy_refuses_a_policy_reference_that_disagrees_with_inline(f):
    _, _, row = admission(f)
    db.execute(
        "UPDATE review_gate_decisions SET policy='{\"changed\":true}' WHERE id=%s", (row["id"],)
    )
    before = rows()
    with pytest.raises(PolicySnapshotUnavailable, match="verification failed"):
        run("copy", row["id"])
    assert rows() == before
    assert db.query_one("SELECT count(*) n FROM review_gate_decision_bodies")["n"] == 0


def test_copy_detects_a_task_url_held_in_both_shapes(f):
    from psycopg.errors import UniqueViolation

    from tests.test_review_decision_storage import reference_only

    task, job, row = admission(f)
    reference_only([row["id"]])
    db.execute(
        "INSERT INTO review_gate_decisions(task_id,url,prompt_hash,stage,mode,action,title,"
        "policy,evidence) VALUES(%s,%s,'p','detailed','off','review','t','{}','{}')",
        (task, job["url"]),
    )
    before = rows()
    with pytest.raises(UniqueViolation):
        run("copy", before[-1]["id"])
    assert rows() == before


def test_bounded_chunks_stop_atomically_at_an_invalid_row(f):
    _, _, first = legacy(f, '{"one":1}')
    _, _, last = legacy(f, '{"two":2}')
    run("copy", last["id"])
    db.execute("UPDATE review_gate_decisions SET title='wrong' WHERE id=%s", (last["id"],))
    before = rows()
    with pytest.raises(PolicySnapshotUnavailable):
        compact(last["id"])
    assert rows() == before
    from api.review_decision_storage import migrate_chunk

    result = migrate_chunk(
        after=0,
        through=last["id"],
        limit=1,
        mode="compact",
        backup_complete=True,
        compatible_readers=True,
    )
    assert result["after"] == first["id"] and result["compacted"] == 1
    assert db.query_one("SELECT title FROM review_gate_decisions WHERE id=%s", (last["id"],)) == {
        "title": "wrong"
    }


def test_concurrent_compaction_counts_each_row_once(f):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, _, row = legacy(f)
    run("copy", row["id"])
    barrier = Barrier(2)

    def once():
        barrier.wait(timeout=10)
        return compact(row["id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: once(), range(2)))
    assert sorted(result["compacted"] for result in results) == [0, 1]
    assert run("verify", row["id"])["verified"] == 1


def test_body_lock_timeout_preserves_inline_values_and_allows_retry(f):
    from concurrent.futures import ThreadPoolExecutor

    from psycopg.errors import LockNotAvailable

    from core.pool import connection

    _, _, row = legacy(f)
    run("copy", row["id"])
    before = rows()

    def blocked():
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='50ms'")
            return compact(row["id"])

    with connection() as conn, conn.transaction():
        conn.execute("SELECT id FROM review_gate_decision_bodies FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(LockNotAvailable):
            executor.submit(blocked).result(timeout=10)
    assert rows() == before
    assert compact(row["id"])["compacted"] == 1


def test_failure_after_clear_rolls_back_the_chunk(f, monkeypatch):
    _, _, row = legacy(f)
    run("copy", row["id"])
    before = rows()
    original = db.execute_count

    def fail_after_write(sql, params=None):
        original(sql, params)
        raise RuntimeError("transaction interrupted")

    monkeypatch.setattr(db, "execute_count", fail_after_write)
    with pytest.raises(RuntimeError, match="transaction interrupted"):
        compact(row["id"])
    assert rows() == before
