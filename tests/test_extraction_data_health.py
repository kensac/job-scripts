from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from api import db
from tasks import comp, requirements


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (-1, -1, (None, None)),
        (-1, 0, (None, 0)),
        (-1, 5, (None, 5)),
        (5, -1, (5, None)),
        (None, -1, (None, None)),
    ],
)
def test_negative_experience_is_unknown_without_reordering_the_valid_bound(low, high, expected):
    parsed = requirements.RequirementsExtract(has_requirements=True, yoe_min=low, yoe_max=high)
    assert requirements._years(parsed) == expected


@pytest.mark.asyncio
async def test_existing_negative_years_are_reextracted_even_when_the_page_hash_matches(
    f, monkeypatch
):
    content = "This position requires five years of experience. " * 20
    _, url = f.make_ready_job(content=content)
    f.make_requirements(
        url, yoe_min=-1, yoe_max=5, content_hash=hashlib.sha256(content.encode()).hexdigest()
    )

    async def answer(task_id, shape, specs):
        return [
            f.make_batch_result(
                task_id,
                spec,
                model="test-model",
                text=json.dumps({"has_requirements": True, "yoe_min": 5}),
                error=None,
            )
            for spec in specs
        ], SimpleNamespace(model="test-model")

    monkeypatch.setattr(requirements, "run_batched", answer)
    task_id = f.make_task("extract_requirements", status="running")
    await requirements.handle_extract_requirements(task_id, {})
    row = db.query_one("SELECT yoe_min, yoe_max FROM job_requirements WHERE url = %s", (url,))
    assert row == {"yoe_min": 5, "yoe_max": None}
    assert (
        db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]["done"]
        == 1
    )

    async def must_not_repeat(*args, **kwargs):
        pytest.fail("A repaired unchanged posting must not be extracted again")

    monkeypatch.setattr(requirements, "run_batched", must_not_repeat)
    await requirements.handle_extract_requirements(
        f.make_task("extract_requirements", status="running"), {}
    )


@pytest.mark.asyncio
async def test_legacy_annual_compensation_is_repaired_from_the_cached_posting_once(f, monkeypatch):
    job_id, _ = f.make_ready_job(content="Salary is CAD 2000 per week. " * 20)
    db.execute(
        "UPDATE jobs SET comp_extracted = true, comp_min = 2000, comp_max = 2000, comp_text = %s, comp_period = NULL, comp_currency = NULL WHERE id = %s",
        ("$2000/week", job_id),
    )

    async def answer(task_id, shape, specs):
        return [
            f.make_batch_result(
                task_id,
                spec,
                model="test-model",
                text=json.dumps(
                    {
                        "has_comp": True,
                        "comp_min": 2000,
                        "comp_max": 2000,
                        "period": "weekly",
                        "currency": "CAD",
                        "basis": "base",
                        "display": "CAD 2000/week",
                    }
                ),
                error=None,
            )
            for spec in specs
        ], SimpleNamespace(model="test-model")

    monkeypatch.setattr(comp, "run_batched", answer)
    task_id = f.make_task("extract_comp", status="running")
    await comp.handle_extract_comp(task_id, {})
    row = db.query_one(
        "SELECT comp_min, comp_max, comp_period, comp_currency, comp_extracted FROM jobs WHERE id = %s",
        (job_id,),
    )
    assert row == {
        "comp_min": 104000,
        "comp_max": 104000,
        "comp_period": "weekly",
        "comp_currency": "CAD",
        "comp_extracted": True,
    }

    async def must_not_repeat(*args, **kwargs):
        pytest.fail("A repaired compensation record must not be extracted again")

    monkeypatch.setattr(comp, "run_batched", must_not_repeat)
    await comp.handle_extract_comp(f.make_task("extract_comp", status="running"), {})


@pytest.mark.asyncio
async def test_a_nonfinite_compensation_answer_does_not_mark_extraction_complete(f, monkeypatch):
    job_id, _ = f.make_ready_job()

    async def answer(task_id, shape, specs):
        return [
            f.make_batch_result(
                task_id,
                spec,
                model="test-model",
                text='{"has_comp":true,"comp_min":NaN,"period":"yearly","currency":"USD"}',
                error=None,
            )
            for spec in specs
        ], SimpleNamespace(model="test-model")

    monkeypatch.setattr(comp, "run_batched", answer)
    task_id = f.make_task("extract_comp", status="running")
    await comp.handle_extract_comp(task_id, {})
    row = db.query_one("SELECT comp_extracted FROM jobs WHERE id = %s", (job_id,))
    assert row["comp_extracted"] is False
    assert (
        db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]["done"]
        == 0
    )
