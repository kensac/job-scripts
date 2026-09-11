import json
from types import SimpleNamespace

import pytest

from api import db
from tasks import runtime


@pytest.mark.asyncio
async def test_mixed_output_and_error_files_checkpoint_and_replay(f, monkeypatch):
    task_id = f.make_task("extract_comp", {"batch_ids": ["mixed"]}, status="running")
    batch = SimpleNamespace(
        id="mixed", status="completed", output_file_id="output", error_file_id="errors"
    )
    files = {
        "output": {
            "custom_id": "success",
            "response": {
                "status_code": 200,
                "body": {
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": "answer"}]}
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            },
        },
        "errors": {"custom_id": "failure", "error": {"code": "invalid_request"}},
    }
    retrieved = []

    async def retrieve(batch_id):
        retrieved.append(batch_id)
        return batch

    async def content(file_id):
        return SimpleNamespace(text=json.dumps(files[file_id]))

    monkeypatch.setattr(
        "core.batch._client",
        lambda: SimpleNamespace(
            batches=SimpleNamespace(retrieve=retrieve), files=SimpleNamespace(content=content)
        ),
    )
    first = await runtime.collect_pending(task_id, None)
    assert {result.custom_id for result in first} == {"success", "failure"}
    assert all(result.batch_id == "mixed" for result in first)
    by_id = {result.custom_id: result for result in first}
    assert by_id["success"].text == "answer"
    assert by_id["success"].usage == {"input_tokens": 10, "output_tokens": 2}
    assert "invalid_request" in by_id["failure"].error
    assert (
        db.query_one("SELECT payload FROM tasks WHERE id=%s", (task_id,))["payload"]["batch_ids"]
        == []
    )
    recovered = await runtime.collect_pending(task_id, None)
    assert recovered == first
    assert retrieved == ["mixed"]
