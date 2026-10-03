import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from api import db
from api.ai import migrate_snapshot_payloads as cli
from api.ai import snapshot_payloads as snapshots
from core.payload_objects import encode_payload
from tests.test_snapshot_payloads import objects, request, row  # noqa: F401


def entry(task_id):
    return {
        "task_id": task_id,
        "custom_id": "request",
        "snapshot_sha256": hashlib.sha256(encode_payload(row(task_id)["snapshot"])).hexdigest(),
    }


def test_manifest_exact_subset_round_trip_and_backup_gate(f, objects):
    tasks = [request(f)[0] for _ in range(3)]
    manifest = [entry(tasks[0]), entry(tasks[2])]
    result = snapshots.migrate_manifest(manifest, objects, mode="copy", limit=2)
    assert result.counts == {"copied": 2}
    assert result.exhausted and result.completed == 2
    assert row(tasks[1])["snapshot_ref"] is None
    with pytest.raises(ValueError, match="backup"):
        snapshots.migrate_manifest(manifest, objects, mode="compact", limit=2)
    for mode in ("verify", "compact", "verify", "restore"):
        result = snapshots.migrate_manifest(
            manifest, objects, mode=mode, limit=2, backup_complete=True
        )
        assert result.exhausted and result.completed == 2
    assert row(tasks[0])["snapshot"] is not None


@pytest.mark.parametrize(
    "failure", ["missing", "active", "profile", "unconsumed", "changed", "concurrent"]
)
def test_manifest_failure_stops_before_gap(f, objects, failure):
    tasks = [request(f)[0] for _ in range(3)]
    manifest = [entry(tid) for tid in tasks]
    if failure == "missing":
        db.execute("DELETE FROM batch_requests WHERE task_id=%s", (tasks[1],))
    elif failure == "active":
        db.execute("UPDATE tasks SET status='pending' WHERE id=%s", (tasks[1],))
    elif failure == "profile":
        db.execute("UPDATE tasks SET kind='classify_job_profiles' WHERE id=%s", (tasks[1],))
    elif failure == "unconsumed":
        from api.ai import batch_results
        from core.batch import BatchResult

        batch_results.checkpoint(tasks[1], [BatchResult("request", batch_id="paid")], [])
    elif failure == "changed":
        db.execute(
            "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s",
            (db.jsonb({"input": "changed"}), tasks[1]),
        )
    else:
        objects.client.after_put = lambda: db.execute(
            "UPDATE tasks SET status='pending' WHERE id=%s", (tasks[1],)
        )
    result = snapshots.migrate_manifest(manifest, objects, mode="copy", limit=3)
    assert not result.exhausted and result.completed == 1
    assert result.after == (tasks[0], "request")
    assert result.failed == (tasks[1], "request")
    assert row(tasks[0])["snapshot_ref"] is not None
    assert row(tasks[2])["snapshot_ref"] is None


@pytest.mark.parametrize("bad", ["reverse", "duplicate", "boolean", "digest", "oversized"])
def test_invalid_manifest_rejected_before_io(f, objects, bad):
    tasks = [request(f)[0] for _ in range(2)]
    manifest = [entry(tid) for tid in tasks]
    if bad == "reverse":
        manifest.reverse()
    elif bad == "duplicate":
        manifest.append(manifest[-1])
    elif bad == "boolean":
        manifest[0]["task_id"] = True
    elif bad == "digest":
        manifest[0]["snapshot_sha256"] = "invalid"
    with pytest.raises(ValueError):
        snapshots.migrate_manifest(
            manifest, objects, mode="copy", limit=1 if bad == "oversized" else 3
        )
    assert objects.client.objects == {}


def test_manifest_cli_single_delta_and_explicit_completion(f, objects, monkeypatch, capsys):
    tasks = [request(f)[0] for _ in range(2)]
    monkeypatch.setattr(cli, "os", SimpleNamespace(environ={}))
    monkeypatch.setattr("core.pool.pool.close", lambda: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([entry(tid) for tid in tasks])))
    monkeypatch.setattr("sys.argv", ["migration", "copy", "--limit", "2", "--manifest-stdin"])
    assert cli.main() == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(reports) == 1
    assert reports[0]["counts"] == {"copied": 2}
    assert reports[0]["exhausted"] is True
    assert reports[0]["completed"] == 2
    assert reports[0]["scope"] == "manifest"
