import hashlib
import json
from types import SimpleNamespace

import pytest

from api import db
from api.tasks import comp, requirements
from core.batch import BatchSpec


def test_requirements_and_skills_rollback_with_the_consuming_transaction(f):
    _, url = f.make_ready_job()
    with pytest.raises(RuntimeError, match="consumer interrupted"), db.transaction():
        requirements._store(
            url,
            requirements.RequirementsExtract(has_requirements=True, skills_required=["Python"]),
            "submitted-hash",
            None,
        )
        raise RuntimeError("consumer interrupted")
    assert db.query_one("SELECT 1 FROM job_requirements WHERE url = %s", (url,)) is None
    assert db.query_one("SELECT 1 FROM job_skills WHERE url = %s", (url,)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["comp", "requirements"])
@pytest.mark.parametrize("changed", [False, True])
async def test_saved_extraction_result_collects_without_new_candidates(
    f, monkeypatch, family, changed
):
    content = (
        "The role requires five years of Python experience and pays USD 100000 per year. " * 20
    )
    job_id, url = f.make_ready_job(content=content)
    content_row = db.query_one(
        "SELECT id FROM ai_queries WHERE url = %s AND check_type = 'content' ORDER BY id DESC LIMIT 1",
        (url,),
    )["id"]
    task_id = f.make_task(f"extract_{family}", status="running")
    context = {
        "job_id": job_id,
        "content_row_id": content_row,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }
    spec = BatchSpec(url, "submitted instructions", content, "Extraction", {}, context=context)
    text = (
        {"has_comp": True, "comp_min": 100000, "period": "yearly", "currency": "USD"}
        if family == "comp"
        else {"has_requirements": True, "yoe_min": 5, "skills_required": ["Python"]}
    )
    result = f.make_batch_result(task_id, spec, text=json.dumps(text), model="submitted-model")
    db.execute("UPDATE jobs SET active = false WHERE id = %s", (job_id,))
    if changed:
        db.execute(
            "INSERT INTO ai_queries (url, check_type, status, input_content) VALUES (%s, 'content', 'passed', %s)",
            (url, "Completely changed posting, no old requirements apply. " * 30),
        )
    module = comp if family == "comp" else requirements
    called = []

    async def collect(tid, shape, specs):
        assert tid == task_id and specs == []
        called.append(tid)
        return [result], SimpleNamespace(model="submitted-model")

    monkeypatch.setattr(module, "run_batched", collect)
    await getattr(module, f"handle_extract_{family}")(task_id, {})
    assert called == [task_id]
    if family == "comp":
        row = db.query_one("SELECT comp_extracted, comp_min FROM jobs WHERE id = %s", (job_id,))
        assert row["comp_extracted"] is (not changed)
        assert row["comp_min"] == (None if changed else 100000)
    else:
        row = db.query_one(
            "SELECT yoe_min, model, content_row_id FROM job_requirements WHERE url = %s", (url,)
        )
        assert (
            row is None
            if changed
            else row == {"yoe_min": 5, "model": "submitted-model", "content_row_id": content_row}
        )
