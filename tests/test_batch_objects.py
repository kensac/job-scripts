"""batch_objects: each bundle's fields held once, member rows pointing at it.

Covers the writer on both sides of the fleet gate, the gate itself, every
phase of tasks.batch_objects, clearing only where the object row is equal,
and that every reader returns the reference the row was written with at
every phase."""

import asyncio
import threading

import pytest

from api import data_level, db, worker
from api.ai import batch_results, request_snapshots
from core.batch import BatchResult, BatchSpec
from core.payload_objects import PayloadStore
from tasks import batch_objects
from tasks.batch_objects import advance
from tests.factories import ObjectClient

FIELDS = request_snapshots.OBJECT_FIELDS


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda **_: store)
    return store


def _worker(name, *, level=data_level.LEVEL, release="r2", written_by="r2", age="0 seconds"):
    db.execute(
        "INSERT INTO worker_status (name, release, data_level, data_level_release, last_seen) "
        "VALUES (%s, %s, %s, %s, now() - %s::interval)",
        (name, release, level, written_by, age),
    )


def _raw(task_id):
    return db.query(
        "SELECT custom_id, snapshot_ref, object_id FROM batch_requests "
        "WHERE task_id = %s ORDER BY custom_id",
        (task_id,),
    )


def _refs(task_id):
    return {r["custom_id"]: r["snapshot_ref"] for r in db.query(request_snapshots.ROWS, (task_id,))}


def _legacy(f, n=3):
    """Rows as the fleet wrote them before batch_objects: the whole reference
    on the row and no pointer."""
    task_id = f.make_task("verify_new", {})
    specs = [
        BatchSpec(f"r{i}", "rules", f"page {i}", "Reply", {"type": "object"}) for i in range(n)
    ]
    batch_results.snapshot_specs(task_id, specs)
    whole = _refs(task_id)
    for custom_id, ref in whole.items():
        db.execute(
            "UPDATE batch_requests SET snapshot_ref = %s, object_id = NULL "
            "WHERE task_id = %s AND custom_id = %s",
            (db.jsonb(ref), task_id, custom_id),
        )
    db.execute("DELETE FROM batch_objects")
    return task_id, specs, whole


def _run(state):
    """advance() until it says stop, recording each phase it passed."""
    phases = [state["phase"]]
    while True:
        state, stop = advance(state)
        phases.append(state["phase"])
        if stop:
            return state, phases


def _resolved(task_id, objects):
    return [
        request_snapshots.resolve(row, objects)
        for row in db.query(request_snapshots.ROWS, (task_id,))
    ]


def test_writer_keeps_the_fields_on_the_row_while_an_older_image_runs(f, objects):
    _worker("old", level=None, release="r1", written_by=None)
    task_id = f.make_task("verify_new", {})
    specs = [BatchSpec("a", "rules", "page", "Reply", {})]
    assert batch_results.snapshot_specs(task_id, specs) == specs
    [row] = _raw(task_id)
    assert set(FIELDS) <= set(row["snapshot_ref"])
    obj = db.query_one("SELECT * FROM batch_objects WHERE id = %s", (row["object_id"],))
    assert {k: obj[k] for k in FIELDS} == {k: row["snapshot_ref"][k] for k in FIELDS}
    assert _resolved(task_id, objects) == specs


def test_writer_stores_only_the_member_once_the_fleet_reads_the_pointer(f, objects):
    _worker("new")
    task_id = f.make_task("verify_new", {})
    specs = [BatchSpec("a", "rules", "page", "Reply", {}), BatchSpec("b", "rules", "p", "R", {})]
    batch_results.snapshot_specs(task_id, specs)
    rows = _raw(task_id)
    assert all(set(r["snapshot_ref"]) == {"member", "member_sha256", "member_size"} for r in rows)
    assert len({r["object_id"] for r in rows}) == 1
    assert db.query_one("SELECT count(*) AS n FROM batch_objects")["n"] == 1
    assert _resolved(task_id, objects) == specs
    # Freezing again reads the existing rows through the pointer.
    assert batch_results.snapshot_specs(task_id, specs) == specs


def test_fleet_gate(f):
    assert data_level.behind(1) == 0
    _worker("current")
    _worker("stopped", level=None, written_by=None, age="10 minutes")
    assert data_level.behind(1) == 0, "a worker not seen for five minutes is not running"
    _worker("old", level=None, release="r1", written_by=None)
    assert data_level.behind(1) == 1
    db.execute("DELETE FROM worker_status WHERE name = 'old'")
    # Rolled back: the older image rewrote release and left the level it never writes.
    _worker("rolled-back", release="r1", written_by="r2")
    assert data_level.behind(1) == 1
    db.execute("DELETE FROM worker_status WHERE name = 'rolled-back'")
    assert data_level.behind(data_level.LEVEL + 1) == 1


def test_phases_end_to_end_and_readers_agree_at_every_phase(f, objects):
    task_id, specs, whole = _legacy(f)
    _worker("old", level=None, release="r1", written_by=None)

    state, phases = _run(dict(batch_objects.START))
    assert phases == ["backfill", "backfill", "gate", "gate"]
    assert state["behind"] == 1
    assert all(r["object_id"] is not None for r in _raw(task_id)), "backfill pointed every row"
    assert all(set(FIELDS) <= set(r["snapshot_ref"]) for r in _raw(task_id)), "nothing cleared"
    assert _refs(task_id) == whole and _resolved(task_id, objects) == specs

    # An older image writes a row after the backfill passed it.
    late = dict(whole["r0"], member="r9")
    db.execute(
        "INSERT INTO batch_requests (task_id, custom_id, snapshot_ref) VALUES (%s, 'r9', %s)",
        (task_id, db.jsonb(late)),
    )
    db.execute(
        "UPDATE worker_status SET data_level = %s, data_level_release = release",
        (data_level.LEVEL,),
    )

    state, phases = _run(state)
    assert phases[:2] == ["gate", "clear"] and phases[-1] == "clear"
    assert state["cleared"] == 0 and state["rows"] == 0, "a pass that cleared rows starts another"
    rows = _raw(task_id)
    assert all(r["object_id"] is not None for r in rows)
    assert all(set(r["snapshot_ref"]) == {"member", "member_sha256", "member_size"} for r in rows)
    assert _refs(task_id) == {**whole, "r9": late}

    state, phases = _run(state)
    assert phases[-1] == "done" and state["cleared"] == 0
    state, phases = _run(state)
    assert phases == ["done", "done"]
    assert _refs(task_id) == {**whole, "r9": late}


def test_clears_only_where_the_object_row_is_equal(f, objects):
    task_id, _, whole = _legacy(f)
    _worker("old", level=None, release="r1", written_by=None)
    state, _ = _run(dict(batch_objects.START))
    assert state["phase"] == "gate"
    db.execute("DELETE FROM worker_status")
    # A row pointed at an object whose fields differ from its own.
    other = db.query_one(
        "INSERT INTO batch_objects (bucket, key, sha256, size, version) "
        "VALUES ('test-payloads', 'elsewhere', %s, 1, 3) RETURNING id",
        ("0" * 64,),
    )["id"]
    db.execute(
        "UPDATE batch_requests SET object_id = %s WHERE task_id = %s AND custom_id = 'r1'",
        (other, task_id),
    )
    # A row whose fields no object row holds: it is never pointed.
    odd = dict(whole["r2"], size=whole["r2"]["size"] + 1)
    db.execute(
        "UPDATE batch_requests SET snapshot_ref = %s, object_id = NULL "
        "WHERE task_id = %s AND custom_id = 'r2'",
        (db.jsonb(odd), task_id),
    )
    for _ in range(3):
        state, phases = _run(state)
        assert phases[-1] == "clear", "never done while a row keeps fields"
    kept = {r["custom_id"]: r for r in _raw(task_id)}
    assert kept["r1"]["snapshot_ref"] == {**whole["r1"]}
    assert kept["r2"]["snapshot_ref"] == odd and kept["r2"]["object_id"] is None
    assert set(kept["r0"]["snapshot_ref"]) == {"member", "member_sha256", "member_size"}


def test_locked_rows_are_skipped_and_cleared_by_a_later_pass(f, objects):
    task_id, _, whole = _legacy(f)
    _worker("old", level=None, release="r1", written_by=None)
    state, _ = _run(dict(batch_objects.START))
    db.execute("DELETE FROM worker_status")
    with db.transaction():
        db.execute(
            "SELECT 1 FROM batch_requests WHERE task_id = %s AND custom_id = 'r1' FOR UPDATE",
            (task_id,),
        )
        # The lock is held by this connection; the chunk runs on another.
        result = {}
        thread = threading.Thread(target=lambda: result.update(s=_run(state)[0]))
        thread.start()
        thread.join()
    assert result["s"]["phase"] == "clear"
    assert set(FIELDS) <= set({r["custom_id"]: r for r in _raw(task_id)}["r1"]["snapshot_ref"])
    state, _ = _run(result["s"])
    state, phases = _run(state)
    assert phases[-1] == "done"
    assert _refs(task_id) == whole


def test_resumes_from_the_last_chunk_and_from_the_previous_run(f, objects):
    _legacy(f, n=5)
    first = batch_objects.chunk(dict(batch_objects.START), clear=False, limit=2)
    assert first["rows"] == 2 and first["c"] == "r1"
    assert batch_objects.chunk(first, clear=False, limit=2)["c"] == "r3"

    # A requeued run resumes from its own payload; a new run from the last run's.
    f.make_task(batch_objects.KIND, {"state": first}, status="failed")
    run = f.make_task(batch_objects.KIND, {})
    assert batch_objects._resume(run, {}) == first
    assert batch_objects._resume(run, {"state": {"phase": "gate"}}) == {"phase": "gate"}
    assert batch_objects._resume(run, {}) != batch_objects.START


def _offered():
    """Pending runs after a scheduling pass; the per-cycle dedupe key is
    cleared first so only the worker's own condition decides."""
    db.execute("UPDATE tasks SET dedupe_key = NULL")
    worker.schedule_ingest_cycle()
    return [
        r["id"]
        for r in db.query(
            "SELECT id FROM tasks WHERE kind = %s AND status = 'pending'", (batch_objects.KIND,)
        )
    ]


def test_the_worker_offers_runs_until_one_reports_done(f, objects):
    task_id, specs, whole = _legacy(f)
    phases = []
    while offered := _offered():
        [run] = offered
        db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (run,))
        asyncio.run(batch_objects.handle_consolidate_batch_objects(run, {}))
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (run,))
        phases.append(db.query_one("SELECT progress FROM tasks WHERE id = %s", (run,))["progress"])
        assert len(phases) < 5
    # The first run backfills, passes the gate and clears; the second clears
    # nothing and is done; no third is offered.
    assert [p["phase"] for p in phases] == ["clear", "done"]
    assert _refs(task_id) == whole and _resolved(task_id, objects) == specs


def test_unconsumed_receipts_resolve_through_the_pointer(f, objects):
    task_id, specs, _ = _legacy(f)
    batch_results.checkpoint(task_id, [BatchResult("r0", text="x", batch_id="b")], [])
    before = batch_results.unconsumed(task_id)
    _run(dict(batch_objects.START))
    _run(dict(batch_objects.START, phase="clear"))
    assert batch_results.unconsumed(task_id) == before
    assert before[0].request == specs[0]
