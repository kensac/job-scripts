import pytest

from api import db, worker
from core.payload_objects import PayloadUnavailable
from tasks import runtime


@pytest.mark.asyncio
async def test_unavailable_snapshot_preserves_paid_batch_provenance(monkeypatch):
    async def unavailable(task_id, payload):
        raise PayloadUnavailable("Required request snapshot unavailable")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", unavailable)
    task_id = runtime.enqueue(
        "test_kind", {"batch_ids": ["paid-batch"], "batch_collection_checkpointed": True}
    )
    await worker.run_once()
    row = db.query_one("SELECT status,payload FROM tasks WHERE id=%s", (task_id,))
    assert row["status"] == "failed"
    assert row["payload"]["batch_ids"] == ["paid-batch"]
    assert row["payload"]["batch_collection_checkpointed"] is True
    assert row["payload"]["payload_recovery"]["reason"] == "payload_unavailable"
