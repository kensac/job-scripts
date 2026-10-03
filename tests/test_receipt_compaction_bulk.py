import json

from api import db
from api.ai import migrate_receipt_payloads
from core.payload_objects import PayloadStore
from tests.test_receipt_verification_bulk import prepare


def test_grouped_compaction_uses_one_conditional_update(f, monkeypatch, capsys):
    from core.pool import pool

    store, rows = prepare(f, 3)
    reads = []
    writes = []
    for name in ("query", "query_one", "execute_count"):
        original = getattr(db, name)

        def measured(sql, params=None, _original=original, _name=name):
            (writes if _name == "execute_count" else reads).append(sql)
            return _original(sql, params)

        monkeypatch.setattr(db, name, measured)
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    monkeypatch.setattr(pool, "close", lambda: None)
    monkeypatch.setenv("PGOPTIONS", "")
    monkeypatch.setattr(
        "sys.argv",
        [
            "migration",
            "compact",
            "--limit",
            "3",
            "--backup-complete",
            "--compact-group-size",
            "3",
            "--compact-workers",
            "2",
            "--compact-byte-budget",
            "100000",
        ],
    )
    assert migrate_receipt_payloads.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"compacted": 3}
    assert result["after"] == [rows[-1]["provider_batch_id"], rows[-1]["custom_id"]]
    assert len(reads) == 4 and len(writes) == 1
    current = db.query("SELECT * FROM batch_result_receipts ORDER BY provider_batch_id,custom_id")
    for original, actual in zip(rows, current, strict=True):
        expected = {**original, "response": dict(original["response"])}
        del expected["response"]["embedding_vectors"]
        assert actual == expected
