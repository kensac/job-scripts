from types import SimpleNamespace

import pytest

from api import db
from api.tasks import locations, verify
from core.batch import BatchSpec
from tests.factories import make_batch_result


def _result(task_id, custom_id, context, text):
    return make_batch_result(
        task_id,
        BatchSpec(
            custom_id, "original instructions", "original input", "Result", {}, context=context
        ),
        text=text,
        batch_id="completed-original-batch",
        model="original-model",
    )


@pytest.mark.asyncio
async def test_location_resume_uses_original_text_when_no_candidates_remain(monkeypatch, f):
    task = f.make_task("classify_locations", {"batch_ids": ["completed-original-batch"]})
    result = _result(
        task,
        "original-location",
        {"text": "Oslo, Norway"},
        '{"places": [{"country": "NO", "region": "", "city": "Oslo"}], "remote": false}',
    )

    async def collect(task_id, shape, specs):
        assert specs == []
        return [result], SimpleNamespace(model="original-model")

    monkeypatch.setattr(locations, "run_batched", collect)
    await locations.handle_classify_locations(task, {})
    row = db.query_one("SELECT country, model FROM locations WHERE text = 'Oslo, Norway'")
    assert row == {"country": "NO", "model": "original-model"}


@pytest.mark.asyncio
async def test_verify_resume_uses_original_subject_when_current_selection_is_empty(monkeypatch, f):
    task = f.make_task("verify_new", {"batch_ids": ["completed-original-batch"]})
    url = "https://example.test/original-posting"
    result = _result(
        task,
        url,
        {
            "company": "Original company",
            "title": "Original title",
            "needs_closed": True,
            "needs_clearance": True,
        },
        '{"is_closed": false, "closed_reason": "open", "requires_clearance_or_restrictions": false, "clearance_reason": "none"}',
    )

    async def collect(task_id, shape, specs):
        assert specs == []
        return [result], SimpleNamespace(model="original-model")

    monkeypatch.setattr(verify, "run_batched", collect, raising=False)
    await verify.handle_verify_new(task, {})
    rows = db.query(
        "SELECT check_type,model,company FROM ai_queries WHERE url = %s ORDER BY check_type", (url,)
    )
    assert rows == [
        {"check_type": "clearance", "model": "original-model", "company": "Original company"},
        {"check_type": "closed", "model": "original-model", "company": "Original company"},
    ]

    await verify.handle_verify_new(task, {})
    assert db.query_one("SELECT count(*) AS n FROM ai_queries WHERE url = %s", (url,))["n"] == 2
    assert (
        db.query_one("SELECT outcome FROM batch_result_receipts WHERE task_id = %s", (task,))[
            "outcome"
        ]
        == "written"
    )
