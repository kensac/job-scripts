import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from api import db
from api.ai import migrate_snapshot_payloads as cli
from api.ai import snapshot_payloads as snapshots
from core.payload_objects import PayloadStore, encode_payload
from tests.factories import ObjectClient
from tests.test_snapshot_payloads import request, row


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda **_: store)
    return store


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
    for item in manifest:
        item["reference"] = row(item["task_id"])["snapshot_ref"]
    for mode in ("verify", "compact", "verify", "restore", "restore"):
        result = snapshots.migrate_manifest(
            manifest, objects, mode=mode, limit=2, backup_complete=True
        )
        assert result.exhausted and result.completed == 2
    assert row(tasks[0])["snapshot"] is not None


@pytest.mark.parametrize("failure", ["missing", "changed", "concurrent"])
def test_manifest_failure_stops_before_gap(f, objects, failure):
    tasks = [request(f)[0] for _ in range(3)]
    manifest = [entry(tid) for tid in tasks]
    if failure == "missing":
        db.execute("DELETE FROM batch_requests WHERE task_id=%s", (tasks[1],))
    elif failure == "changed":
        db.execute(
            "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s",
            (db.jsonb({"input": "changed"}), tasks[1]),
        )
    else:
        objects.client.after_put = lambda: db.execute(
            "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s",
            (db.jsonb({"input": "changed"}), tasks[1]),
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


@pytest.mark.parametrize("field", ["metadata_md5", "reference"])
def test_manifest_rejects_frozen_evidence_drift(f, objects, field):
    task_id, _ = request(f)
    manifest = [entry(task_id)]
    snapshots.migrate(row(task_id), objects, mode="copy")
    if field == "metadata_md5":
        manifest[0][field] = "0" * 32
    else:
        from dataclasses import asdict

        manifest[0][field] = asdict(objects.put_verified({"different": "payload"}))
    original = row(task_id)
    result = snapshots.migrate_manifest(
        manifest, objects, mode="compact", limit=1, backup_complete=True
    )
    assert result.counts == {"changed": 1}
    assert result.completed == 0 and not result.exhausted
    assert row(task_id) == original


def test_manifest_missing_object_and_resume_preserve_exact_prefix(f, objects):
    tasks = [request(f)[0] for _ in range(2)]
    # Distinct requests give independent object failure domains.
    db.execute(
        "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s",
        (db.jsonb({"input": "second"}), tasks[1]),
    )
    manifest = [entry(tid) for tid in tasks]
    snapshots.migrate_manifest(manifest, objects, mode="copy", limit=2)
    second = row(tasks[1])
    stored = objects.client.objects.pop(
        (second["snapshot_ref"]["bucket"], second["snapshot_ref"]["key"])
    )
    result = snapshots.migrate_manifest(manifest, objects, mode="verify", limit=2)
    assert result.completed == 1 and result.failed == (tasks[1], "request")
    objects.client.objects[(second["snapshot_ref"]["bucket"], second["snapshot_ref"]["key"])] = (
        stored
    )
    resumed = snapshots.migrate_manifest(manifest[1:], objects, mode="verify", limit=1)
    assert resumed.completed == 1 and resumed.exhausted
