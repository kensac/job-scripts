import json

from api import db
from api.ai import migrate_receipt_payloads, receipt_payloads
from core.payload_objects import PayloadStore
from tests.test_receipt_payloads import ObjectClient, receipt, row


def prepare(f, count):
    store = PayloadStore(ObjectClient(), "test-payloads")
    tasks = []
    for _ in range(count):
        task, _ = receipt(f)
        receipt_payloads.migrate(row(task), store, mode="copy")
        tasks.append(task)
    return store, sorted(
        (row(task) for task in tasks),
        key=lambda item: (item["provider_batch_id"], item["custom_id"]),
    )


def cli(monkeypatch, store, *options):
    from core.pool import pool

    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    monkeypatch.setattr(pool, "close", lambda: None)
    monkeypatch.setenv("PGOPTIONS", "")
    monkeypatch.setattr("sys.argv", ["migration", "verify", "--limit", "3", *options])
    return migrate_receipt_payloads.main()


def test_serial_verification_stops_before_changed_row(f, monkeypatch, capsys):
    store, rows = prepare(f, 3)
    original = store.get
    calls = 0

    def change_during_get(ref):
        nonlocal calls
        calls += 1
        result = original(ref)
        if calls == 2:
            db.execute("UPDATE tasks SET status='running' WHERE id=%s", (rows[1]["task_id"],))
        return result

    monkeypatch.setattr(store, "get", change_during_get)
    assert cli(monkeypatch, store) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["after"] == [rows[0]["provider_batch_id"], rows[0]["custom_id"]]
    assert result["counts"] == {"verified": 1, "changed": 1}
    assert calls == 2


def test_grouped_verification_bounds_database_queries(f, monkeypatch, capsys):
    store, rows = prepare(f, 3)
    calls = []
    for name in ("query", "query_one"):
        original = getattr(db, name)

        def measured(sql, params=None, _original=original):
            calls.append(sql)
            return _original(sql, params)

        monkeypatch.setattr(db, name, measured)
    assert (
        cli(
            monkeypatch,
            store,
            "--verify-group-size",
            "3",
            "--verify-workers",
            "2",
            "--verify-byte-budget",
            "100000",
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"verified": 3}
    assert result["after"] == [rows[-1]["provider_batch_id"], rows[-1]["custom_id"]]
    assert len(calls) == 3
