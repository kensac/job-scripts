"""The bundle backfill: one object per task page instead of one per row."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from api import db
from api.ai import batch_results, request_snapshots, snapshot_payloads
from api.ai import migrate_snapshot_payloads as cli
from core.batch import BatchResult
from core.payload_objects import PayloadStore
from tests.test_snapshot_bundles import CountingClient, rows, task


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(CountingClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
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
    monkeypatch.setattr(snapshot_payloads, "BUNDLE_MAX_BYTES", 2 * size + 50)
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


@pytest.mark.parametrize("change", ["reactivate", "new_receipt"])
def test_bundle_skips_a_task_that_lost_eligibility_after_upload(f, objects, change):
    task_id, _ = task(f, 2)

    def mutate():
        if change == "reactivate":
            db.execute("UPDATE tasks SET status='pending' WHERE id=%s", (task_id,))
        else:
            batch_results.checkpoint(task_id, [BatchResult("r0", batch_id="paid")], [])

    objects.client.after_put = mutate
    assert snapshot_payloads.bundle_many(rows(task_id), objects) == ["changed"] * 2
    assert all(source["snapshot_ref"] is None for source in rows(task_id))


def test_failed_upload_writes_no_reference(f, objects):
    task_id, _ = task(f, 2)
    original = rows(task_id)
    objects.client.fail_put = True
    assert snapshot_payloads.bundle_many(original, objects) == ["unavailable"]
    assert rows(task_id) == original


def test_bundle_leaves_v2_and_profile_and_active_rows_alone(f, objects, monkeypatch, capsys):
    task_id, _ = task(f, 3)
    v2 = objects.put_verified(rows(task_id)[1]["snapshot"])
    db.execute(
        "UPDATE batch_requests SET snapshot_ref=%s WHERE task_id=%s AND custom_id='r1'",
        (db.jsonb(asdict(v2)), task_id),
    )
    active, _ = task(f, 1, status="running")
    profile, _ = task(f, 1)
    db.execute("UPDATE tasks SET kind='classify_job_profiles' WHERE id=%s", (profile,))
    untouched = rows(task_id)[1], rows(active), rows(profile)
    code, report = run(monkeypatch, capsys, "bundle", "--limit", "10")
    assert code == 0 and report["counts"] == {"bundled": 2}
    assert (rows(task_id)[1], rows(active), rows(profile)) == untouched
    assert [s["snapshot_ref"]["version"] for s in rows(task_id)] == [3, 2, 3]


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
