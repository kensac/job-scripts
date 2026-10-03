"""The bundle backfill: one object per task page instead of one per row."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from api import db
from api.ai import batch_results, request_snapshots, snapshot_payloads
from api.ai import migrate_snapshot_payloads as cli
from core import payload_objects
from core.batch import BatchResult
from core.payload_objects import PayloadRef, PayloadStore
from tests.test_snapshot_bundles import CountingClient, rows, task


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(CountingClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda **_: store)
    return store


def run(monkeypatch, capsys, *args):
    monkeypatch.setattr(cli, "os", SimpleNamespace(environ={}))
    # The test harness owns the shared pool; the deployed CLI owns its own.
    monkeypatch.setattr("core.pool.pool.close", lambda: None)
    monkeypatch.setattr("sys.argv", ["migration", *args])
    code = cli.main()
    return code, json.loads(capsys.readouterr().out.splitlines()[-1])


def bundles(store):
    return {key for _, key in store.client.objects if key.startswith("payloads/v3/")}


def test_a_task_with_one_row_over_the_bundle_size_makes_two_bundles(
    f, objects, monkeypatch, capsys
):
    task_id, specs = task(f, 4)
    other, _ = task(f, 1)
    code, report = run(monkeypatch, capsys, "bundle", "--limit", "10", "--chunk-size", "3")
    assert code == 0
    assert report["counts"] == {"bundled": 5}
    after = rows(task_id)
    assert all(source["snapshot"] is not None for source in after)
    assert all(source["snapshot_ref"]["version"] == 3 for source in after)
    assert len({source["snapshot_ref"]["key"] for source in after}) == 2
    # One task per bundle: the next task's row is never packed with this one's.
    assert rows(other)[0]["snapshot_ref"]["key"] not in {s["snapshot_ref"]["key"] for s in after}
    assert len(bundles(objects)) == 3
    assert len(objects.client.objects) == 3
    assert [request_snapshots.resolve({**s, "snapshot": None}) for s in after] == specs


def test_bundle_bytes_split_a_page(f, objects, monkeypatch):
    task_id, _ = task(f, 4)
    size = len(json.dumps(rows(task_id)[0]["snapshot"]))
    monkeypatch.setattr(payload_objects, "BUNDLE_MAX_BYTES", 2 * size + 50)
    assert snapshot_payloads.bundle_many(rows(task_id), objects) == ["bundled"] * 4
    assert len({source["snapshot_ref"]["key"] for source in rows(task_id)}) == 2


def test_bundle_skips_rows_changed_between_upload_and_lock(f, objects):
    task_id, _ = task(f, 3)
    sources = rows(task_id)

    def edit():
        db.execute(
            "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s AND custom_id='r1'",
            (db.jsonb({"input": "edited"}), task_id),
        )

    objects.client.after_put = edit
    assert snapshot_payloads.bundle_many(sources, objects, workers=2) == [
        "bundled",
        "changed",
        "bundled",
    ]
    after = rows(task_id)
    assert after[1]["snapshot_ref"] is None
    assert after[1]["snapshot"]["input"] == "edited"
    assert [source["snapshot"] for source in (after[0], after[2])] == [
        sources[0]["snapshot"],
        sources[2]["snapshot"],
    ]
    assert after[0]["snapshot_ref"]["member"] == "r0"


def resolved(task_id):
    return [request_snapshots.resolve(source) for source in rows(task_id)]


@pytest.mark.parametrize("mode", ["bundle", "compact"])
def test_consumption_running_beside_the_migration_reads_the_same_requests(
    f, objects, monkeypatch, capsys, mode
):
    """A live task's request is immutable: whichever side of the swap a reader
    lands on, inline or reference, it resolves to the same request, and the
    migration takes no lock that consumption takes."""
    task_id, specs = task(f, 3, status="awaiting_batch")
    batch_results.checkpoint(
        task_id, [BatchResult(s.custom_id, text="{}", batch_id="paid") for s in specs], []
    )
    if mode == "compact":
        assert run(monkeypatch, capsys, "bundle", "--limit", "10")[0] == 0
    seen = []

    def consume():
        results = batch_results.unconsumed(task_id)
        seen.append([result.request for result in results])
        if results:
            with batch_results.consume_result(task_id, results[0]) as receipt:
                receipt.outcome = "written"
        batch_results.checkpoint(task_id, [BatchResult("r9", text="{}", batch_id="late")], [])

    if mode == "bundle":
        objects.client.after_put = consume
    else:
        real = snapshot_payloads._locked
        monkeypatch.setattr(snapshot_payloads, "_locked", lambda work: (consume(), real(work))[1])
    args = ("--backup-complete",) if mode == "compact" else ()
    code, report = run(monkeypatch, capsys, mode, "--limit", "10", *args)
    assert code == 0
    assert report["counts"] == {"bundled" if mode == "bundle" else "compacted": 3}
    assert seen[0] == specs
    # The late receipt names no request ("late" sorts before "paid").
    assert [result.request for result in batch_results.unconsumed(task_id)] == [None, *specs[1:]]
    assert resolved(task_id) == specs
    assert all(source["snapshot_ref"]["version"] == 3 for source in rows(task_id))


def test_failed_upload_writes_no_reference(f, objects):
    task_id, _ = task(f, 2)
    original = rows(task_id)
    objects.client.fail_put = True
    assert snapshot_payloads.bundle_many(original, objects) == ["unavailable"]
    assert rows(task_id) == original


def every_population(f, objects):
    """One task of each shape production holds, with distinct content."""
    tasks = {
        "done": task(f, 3)[0],
        "failed": task(f, 1, status="failed")[0],
        "cancelled": task(f, 1, status="cancelled")[0],
        "running": task(f, 1, status="running")[0],
        "profile": task(f, 1)[0],
        "unconsumed": task(f, 1)[0],
    }
    db.execute("UPDATE tasks SET kind='classify_job_profiles' WHERE id=%s", (tasks["profile"],))
    batch_results.checkpoint(tasks["unconsumed"], [BatchResult("r0", batch_id="paid")], [])
    db.execute(
        "UPDATE batch_requests SET snapshot=jsonb_set(snapshot,'{context,task}',to_jsonb(task_id))"
    )
    done = rows(tasks["done"])
    # A per-row version 2 reference beside its inline value, and one compacted.
    for source, keep_inline in ((done[1], True), (done[2], False)):
        ref = objects.put_verified(source["snapshot"])
        db.execute(
            "UPDATE batch_requests SET snapshot_ref=%s"
            + ("" if keep_inline else ",snapshot=NULL")
            + " WHERE task_id=%s AND custom_id=%s",
            (db.jsonb(asdict(ref)), source["task_id"], source["custom_id"]),
        )
    return tasks


def test_bundle_takes_every_row_and_repoints_per_row_references(f, objects, monkeypatch, capsys):
    tasks = every_population(f, objects)
    original = {name: rows(task_id) for name, task_id in tasks.items()}
    specs = {name: resolved(task_id) for name, task_id in tasks.items()}
    v2_objects = {key for _, key in objects.client.objects if key.startswith("payloads/v2/")}
    code, report = run(monkeypatch, capsys, "bundle", "--limit", "20")
    assert code == 0 and report["counts"] == {"bundled": 8}
    after = {name: rows(task_id) for name, task_id in tasks.items()}
    assert all(s["snapshot_ref"]["version"] == 3 for group in after.values() for s in group)
    # Bundling never changes the inline value a reader already prefers.
    assert {n: [s["snapshot"] for s in g] for n, g in after.items()} == {
        n: [s["snapshot"] for s in g] for n, g in original.items()
    }
    assert {name: resolved(task_id) for name, task_id in tasks.items()} == specs
    assert {
        name: [request_snapshots.resolve({**s, "snapshot": None}) for s in group]
        for name, group in after.items()
    } == specs
    # The superseded per-row objects stay where they were.
    assert v2_objects <= {key for _, key in objects.client.objects}
    # A rerun finds nothing left to bundle.
    assert run(monkeypatch, capsys, "bundle", "--limit", "20")[1]["counts"] == {}

    code, report = run(monkeypatch, capsys, "compact", "--limit", "20", "--backup-complete")
    assert code == 0 and report["counts"] == {"compacted": 7}
    inline, not_member = db.query_one(
        "SELECT count(*) FILTER (WHERE snapshot IS NOT NULL) AS a, "
        "count(*) FILTER (WHERE snapshot_ref->>'version' IS DISTINCT FROM '3') AS b "
        "FROM batch_requests"
    ).values()
    assert (inline, not_member) == (0, 0)
    assert {name: resolved(task_id) for name, task_id in tasks.items()} == specs

    code, report = run(monkeypatch, capsys, "restore", "--limit", "20")
    assert code == 0 and report["counts"] == {"restored": 8}
    restored = {name: rows(task_id) for name, task_id in tasks.items()}
    assert {n: [s["snapshot"] for s in g] for n, g in restored.items()} == {
        n: [s["snapshot"] or objects.get(PayloadRef.parse(s["snapshot_ref"])) for s in g]
        for n, g in original.items()
    }
    assert all(s["snapshot_ref"] is None for g in restored.values() for s in g)


def test_repointing_a_reference_that_changed_after_upload_is_skipped(f, objects):
    task_id, _ = task(f, 2)
    first = rows(task_id)[0]
    ref = objects.put_verified(first["snapshot"])
    db.execute(
        "UPDATE batch_requests SET snapshot=NULL,snapshot_ref=%s WHERE task_id=%s "
        "AND custom_id='r0'",
        (db.jsonb(asdict(ref)), task_id),
    )
    sources = rows(task_id)
    other = objects.put_verified({**first["snapshot"], "input": "elsewhere"})
    objects.client.after_put = lambda: db.execute(
        "UPDATE batch_requests SET snapshot_ref=%s WHERE task_id=%s AND custom_id='r0'",
        (db.jsonb(asdict(other)), task_id),
    )
    assert snapshot_payloads.bundle_many(sources, objects) == ["changed", "bundled"]
    assert rows(task_id)[0]["snapshot_ref"] == asdict(other)


def test_an_unreadable_per_row_reference_stops_the_page(f, objects):
    task_id, _ = task(f, 2)
    first = rows(task_id)[0]
    ref = objects.put_verified(first["snapshot"])
    db.execute(
        "UPDATE batch_requests SET snapshot=NULL,snapshot_ref=%s WHERE task_id=%s "
        "AND custom_id='r0'",
        (db.jsonb(asdict(ref)), task_id),
    )
    before = rows(task_id)
    objects.client.objects.clear()
    assert snapshot_payloads.bundle_many(before, objects) == ["unavailable"]
    assert rows(task_id) == before


def test_bundle_then_verify_compact_restore_round_trip(f, objects, monkeypatch, capsys):
    tasks = [task(f, 3)[0] for _ in range(2)]
    # Distinct content, or the two tasks' bundles are one content-addressed object.
    db.execute(
        "UPDATE batch_requests SET snapshot=jsonb_set(snapshot,'{context,task}',to_jsonb(task_id))"
    )
    original = [rows(task_id) for task_id in tasks]
    assert run(monkeypatch, capsys, "bundle", "--limit", "10")[0] == 0
    assert run(monkeypatch, capsys, "verify", "--limit", "10")[1]["counts"] == {"verified": 6}
    objects.client.gets.clear()
    code, report = run(
        monkeypatch, capsys, "compact", "--limit", "10", "--chunk-size", "6", "--backup-complete"
    )
    assert code == 0 and report["counts"] == {"compacted": 6}
    assert len(objects.client.gets) == 2
    assert all(s["snapshot"] is None for task_id in tasks for s in rows(task_id))
    code, report = run(monkeypatch, capsys, "restore", "--limit", "10")
    assert code == 0 and report["counts"] == {"restored": 6}
    assert [rows(task_id) for task_id in tasks] == original


def test_cli_reports_skipped_rows_as_a_failure(f, objects, monkeypatch, capsys):
    task_id, _ = task(f, 2)
    later, _ = task(f, 1)
    objects.client.after_put = lambda: db.execute(
        "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s AND custom_id='r0'",
        (db.jsonb({"input": "edited"}), task_id),
    )
    code, report = run(monkeypatch, capsys, "bundle", "--limit", "10")
    assert code == 1
    assert report["counts"] == {"changed": 1, "bundled": 2}
    assert rows(later)[0]["snapshot_ref"] is not None


def test_cli_stops_before_an_unavailable_page(f, objects, monkeypatch, capsys):
    first, _ = task(f, 1)
    task(f, 1)
    objects.client.fail_put = True
    code, report = run(monkeypatch, capsys, "bundle", "--limit", "10")
    assert code == 1
    assert report["after"] is None and report["counts"] == {"unavailable": 1}
    assert rows(first)[0]["snapshot_ref"] is None


def distinct_tasks(f, count):
    tasks = [task(f, 2)[0] for _ in range(count)]
    # Distinct content, or the tasks' bundles are one content-addressed object.
    db.execute(
        "UPDATE batch_requests SET snapshot=jsonb_set(snapshot,'{context,task}',to_jsonb(task_id))"
    )
    return tasks


def test_concurrent_pages_each_commit_and_report_in_cursor_order(f, objects, monkeypatch, capsys):
    import threading
    import time

    tasks = distinct_tasks(f, 6)
    lock = threading.Lock()
    active = peak = 0
    original_put = objects.client.put_object

    def slow_put(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            # Simulated latency releases the interpreter so pages overlap.
            time.sleep(0.02)
            return original_put(**kwargs)
        finally:
            with lock:
                active -= 1

    objects.client.put_object = slow_put
    monkeypatch.setattr(cli, "os", SimpleNamespace(environ={}))
    monkeypatch.setattr("core.pool.pool.close", lambda: None)
    monkeypatch.setattr("sys.argv", ["migration", "bundle", "--limit", "20", "--workers", "4"])
    assert cli.main() == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # One progress line per committed page, in cursor order, then the summary.
    assert [line["after"] for line in lines] == [[t, "r1"] for t in tasks] + [[tasks[-1], "r1"]]
    assert lines[-1]["counts"] == {"bundled": 12}
    assert 1 < peak <= 4
    assert all(s["snapshot_ref"]["version"] == 3 for t in tasks for s in rows(t))


def test_failed_middle_page_keeps_cursor_before_it_and_rerun_closes_the_gap(
    f, objects, monkeypatch, capsys
):
    tasks = distinct_tasks(f, 5)
    failing = tasks[2]
    original_put = objects.client.put_object

    def put(**kwargs):
        if f'"task":{failing}}}'.encode() in kwargs["Body"]:
            raise OSError("upload failed")
        return original_put(**kwargs)

    objects.client.put_object = put
    code, report = run(monkeypatch, capsys, "bundle", "--limit", "20", "--workers", "4")
    assert code == 1
    assert report["after"] == [tasks[1], "r1"]
    assert report["counts"]["unavailable"] == 1
    assert all(s["snapshot_ref"] is None for s in rows(failing))
    # Pages already in flight past the failure commit on their own; the cursor
    # does not pass the failure, and a rerun from it finds them idempotently.
    assert all(s["snapshot_ref"] is not None for t in tasks[3:] for s in rows(t))
    objects.client.put_object = original_put
    code, report = run(
        monkeypatch,
        capsys,
        "bundle",
        "--limit",
        "20",
        "--workers",
        "4",
        "--after",
        str(tasks[1]),
        "r1",
    )
    assert code == 0 and report["counts"] == {"bundled": 2}
    assert all(s["snapshot_ref"]["version"] == 3 for t in tasks for s in rows(t))


@pytest.mark.parametrize("mode", ["bundle", "verify"])
def test_pages_of_one_task_run_side_by_side(f, objects, monkeypatch, capsys, mode):
    """Production 2026-10-03: pages of one task locked its tasks row together
    and queued past the 2 s lock_timeout, aborting the whole invocation. Pages
    now lock only their own request rows, so they overlap without waiting."""
    import threading
    import time

    # Five rows in pages of two: in verify mode the third page spans two tasks.
    big, _ = task(f, 5)
    small = distinct_tasks(f, 3)
    if mode == "verify":
        assert run(monkeypatch, capsys, "bundle", "--limit", "20")[0] == 0
    name = "bundle_many" if mode == "bundle" else "migrate_many"
    real = getattr(snapshot_payloads, name)
    lock = threading.Lock()
    active: dict[int, int] = {}
    peak_task = peak = 0

    def instrumented(sources, *args, **kwargs):
        nonlocal peak_task, peak
        ids = {source["task_id"] for source in sources}
        with lock:
            for task_id in ids:
                active[task_id] = active.get(task_id, 0) + 1
                peak_task = max(peak_task, active[task_id])
            peak = max(peak, sum(1 for count in active.values() if count))
        try:
            time.sleep(0.05)
            return real(sources, *args, **kwargs)
        finally:
            with lock:
                for task_id in ids:
                    active[task_id] -= 1

    monkeypatch.setattr(snapshot_payloads, name, instrumented)
    code, report = run(
        monkeypatch, capsys, mode, "--limit", "20", "--chunk-size", "2", "--workers", "4"
    )
    assert code == 0
    assert report["counts"] == {"bundled" if mode == "bundle" else "verified": 11}
    assert report["after"] == [small[-1], "r1"]
    assert peak_task > 1
    assert all(s["snapshot_ref"]["version"] == 3 for t in [big, *small] for s in rows(t))


def lock_failures(monkeypatch, failing, times):
    """Fail task `failing`'s locked recheck the way Postgres does past lock_timeout."""
    import psycopg

    real = snapshot_payloads._current_sources
    calls = {"locked": 0}

    def current(sources, *, lock=False):
        if lock and sources[0]["task_id"] == failing:
            calls["locked"] += 1
            if calls["locked"] <= times:
                raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
        return real(sources, lock=lock)

    monkeypatch.setattr(snapshot_payloads, "_current_sources", current)
    monkeypatch.setattr(snapshot_payloads, "LOCK_RETRY_DELAYS", (0.0, 0.0))
    return calls


@pytest.mark.parametrize("mode", ["bundle", "copy"])
def test_a_lock_timeout_on_a_page_is_retried(f, objects, monkeypatch, capsys, mode):
    tasks = distinct_tasks(f, 3)
    calls = lock_failures(monkeypatch, tasks[1], times=1)
    code, report = run(
        monkeypatch, capsys, mode, "--limit", "20", "--chunk-size", "2", "--workers", "2"
    )
    assert code == 0
    assert report["counts"] == {"bundled" if mode == "bundle" else "copied": 6}
    assert calls["locked"] == 2
    assert all(s["snapshot_ref"] is not None for t in tasks for s in rows(t))


@pytest.mark.parametrize("mode", ["bundle", "copy"])
def test_a_persistent_lock_timeout_stops_the_cursor_before_its_page(
    f, objects, monkeypatch, capsys, mode
):
    tasks = distinct_tasks(f, 3)
    calls = lock_failures(monkeypatch, tasks[1], times=99)
    monkeypatch.setattr(cli, "os", SimpleNamespace(environ={}))
    monkeypatch.setattr("core.pool.pool.close", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        ["migration", mode, "--limit", "20", "--chunk-size", "2", "--workers", "2"],
    )
    assert cli.main() == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert calls["locked"] == 3
    assert [line for line in lines if "error" in line] == [
        {
            "mode": mode,
            "error": "lock_timeout",
            "at": [tasks[1], "r0"],
            "detail": "canceling statement due to lock timeout",
        }
    ]
    report = lines[-1]
    assert report["after"] == [tasks[0], "r1"]
    assert report["counts"]["lock_timeout"] == 1
    assert all(s["snapshot_ref"] is None for s in rows(tasks[1]))
    # The page after it was in flight and commits; a rerun finds it done.
    assert all(s["snapshot_ref"] is not None for t in (tasks[0], tasks[2]) for s in rows(t))
