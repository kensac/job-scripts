import json
from types import SimpleNamespace

import pytest

from api import db
from api.tasks import comp


@pytest.mark.asyncio
async def test_content_committed_after_guard_does_not_leave_stale_comp_complete(f, monkeypatch):
    job_id, _url = f.make_ready_job(content="Salary USD 100000 yearly. " * 20)
    old_guard = comp.rescrape.content_is_current

    def guard_then_concurrent_scrape(url, content_row_id):
        current = old_guard(url, content_row_id)
        if current:
            # A separate thread has no receipt transaction ContextVar, so this
            # commits through an independent database connection.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(
                    f.make_verdict, url, "content", content="Salary USD 200000 yearly. " * 20
                ).result()
        return current

    async def answer(task_id, shape, specs):
        return [
            f.make_batch_result(
                task_id,
                spec,
                model="test-model",
                text=json.dumps(
                    {"has_comp": True, "comp_min": 100000, "period": "yearly", "currency": "USD"}
                ),
                error=None,
            )
            for spec in specs
        ], SimpleNamespace(model="test-model")

    monkeypatch.setattr(comp, "run_batched", answer)
    monkeypatch.setattr(comp.rescrape, "content_is_current", guard_then_concurrent_scrape)
    await comp.handle_extract_comp(f.make_task("extract_comp", status="running"), {})
    row = db.query_one("SELECT comp_min, comp_extracted FROM jobs WHERE id = %s", (job_id,))
    assert row != {"comp_min": 100000, "comp_extracted": True}


@pytest.mark.asyncio
async def test_known_compensation_source_changes_are_selected_again(f, monkeypatch):
    job_id, url = f.make_ready_job(content="Salary USD 100000 yearly. " * 20)
    requested = []

    async def answer(task_id, shape, specs):
        requested.extend(specs)
        return [
            f.make_batch_result(
                task_id,
                spec,
                model="test-model",
                text=json.dumps(
                    {
                        "has_comp": True,
                        "comp_min": 200000 if "200000" in spec.input else 100000,
                        "period": "yearly",
                        "currency": "USD",
                    }
                ),
                error=None,
            )
            for spec in specs
        ], SimpleNamespace(model="test-model")

    monkeypatch.setattr(comp, "run_batched", answer)
    await comp.handle_extract_comp(f.make_task("extract_comp", status="running"), {})
    assert len(requested) == 1
    f.make_verdict(url, "content", content="Salary USD 200000 yearly. " * 20)
    await comp.handle_extract_comp(f.make_task("extract_comp", status="running"), {})
    assert len(requested) == 2
    assert db.query_one("SELECT comp_min FROM jobs WHERE id = %s", (job_id,))["comp_min"] == 200000
    await comp.handle_extract_comp(f.make_task("extract_comp", status="running"), {})
    assert len(requested) == 2


@pytest.mark.asyncio
async def test_unknown_legacy_source_does_not_trigger_bulk_reextraction(f, monkeypatch):
    job_id, url = f.make_ready_job()
    db.execute(
        "UPDATE jobs SET comp_extracted = true, comp_min = 100000, comp_period = 'yearly' WHERE id = %s",
        (job_id,),
    )
    f.make_verdict(url, "content", content="New posting content " * 20)

    async def unexpected(*args):
        pytest.fail("Unknown legacy source must not trigger extraction")

    monkeypatch.setattr(comp, "run_batched", unexpected)
    await comp.handle_extract_comp(f.make_task("extract_comp", status="running"), {})
