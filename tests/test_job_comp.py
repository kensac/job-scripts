"""Pay is written to job_comp, and the copy task brings job_comp level with
the pay still stored on jobs."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from api import db
from core.comp import COMP_INPUT_CHARS
from tasks import comp, comp_copy

_PAY = ("comp_min", "comp_max", "comp_text", "comp_period", "comp_currency", "comp_basis")


def _pay(table: str, url: str) -> dict | None:
    key = "url"
    row_id = "content_row_id" if table == "job_comp" else "comp_content_row_id"
    return db.query_one(
        f"SELECT {', '.join(_PAY)}, {row_id} AS content_row_id FROM {table} WHERE {key} = %s",
        (url,),
    )


@pytest.mark.asyncio
async def test_the_sweep_writes_job_comp_beside_jobs_with_the_hash_of_what_was_read(f, monkeypatch):
    # Multibyte text past the cut, so a hash of bytes or of the whole page
    # would differ from the hash of the characters the model read.
    content = "Salaire 120 000 € par année, équipe à Montréal. " * 500
    _, url = f.make_ready_job(content=content)

    async def answer(task_id, shape, specs):
        return [
            f.make_batch_result(
                task_id,
                spec,
                model="test-model",
                text=json.dumps(
                    {
                        "has_comp": True,
                        "comp_min": 120000,
                        "period": "yearly",
                        "currency": "EUR",
                        "display": "120 000 €",
                    }
                ),
            )
            for spec in specs
        ], SimpleNamespace(model="test-model")

    monkeypatch.setattr("tasks.derive.run_batched", answer)
    await comp.PAY.handle(f.make_task("extract_comp", status="running"), {})

    assert _pay("job_comp", url) == _pay("jobs", url)
    assert _pay("job_comp", url)["comp_min"] == 120000
    row = db.query_one("SELECT model, content_hash FROM job_comp WHERE url = %s", (url,))
    assert row["model"] == "test-model"
    assert (
        row["content_hash"]
        == hashlib.sha256(content[:COMP_INPUT_CHARS].encode("utf-8")).hexdigest()
    )


def test_the_copy_brings_job_comp_level_with_jobs_and_then_copies_nothing(f):
    _, only_jobs = f.make_ready_job()
    _, differs = f.make_ready_job()
    _, level = f.make_ready_job()
    _, never = f.make_ready_job()
    db.execute(
        "UPDATE jobs SET comp_extracted = true, comp_min = 90000, comp_max = 100000, "
        "comp_period = 'yearly', comp_currency = 'USD' WHERE url = ANY(%s)",
        ([only_jobs, differs, level],),
    )
    db.execute(
        "INSERT INTO job_comp (url, comp_min, comp_max, comp_period, comp_currency, model, "
        "content_hash) VALUES (%s, 1, 2, 'yearly', 'USD', 'm', 'h'), "
        "(%s, 90000, 100000, 'yearly', 'USD', 'kept', 'kept')",
        (differs, level),
    )
    assert db.query_one(comp_copy.DIFFERING)["n"] == 2

    assert comp_copy.copy_batch(0, limit=10_000)[1] == 2
    assert db.query_one(comp_copy.DIFFERING)["n"] == 0
    for url in (only_jobs, differs, level):
        assert _pay("job_comp", url) == _pay("jobs", url)
    assert _pay("job_comp", never) is None
    # An equal row keeps what only job_comp knows; a replaced one loses it.
    provenance = {
        r["url"]: (r["model"], r["content_hash"])
        for r in db.query("SELECT url, model, content_hash FROM job_comp")
    }
    assert provenance[level] == ("kept", "kept")
    assert provenance[differs] == (None, None)

    assert comp_copy.copy_batch(0, limit=10_000)[1] == 0


@pytest.mark.asyncio
async def test_a_run_reports_what_it_copied_and_what_still_differs(f):
    _, url = f.make_ready_job()
    db.execute("UPDATE jobs SET comp_extracted = true, comp_min = 50000 WHERE url = %s", (url,))
    task_id = f.make_task("copy_job_comp", status="running")
    await comp_copy.handle_copy_job_comp(task_id, {})
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert (progress["copied"], progress["differing"]) == (1, 0)
