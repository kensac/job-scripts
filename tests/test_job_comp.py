"""Pay is a derived fact in job_comp: written by the comp derivation with the
hash of the text it read, and not paid for again when a re-fetch changed
nothing."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from api import db
from core.comp import COMP_INPUT_CHARS
from tasks import comp

# Multibyte text past the cut, so a hash of bytes or of the whole page would
# differ from the hash of the characters the model read.
CONTENT = "Salaire 120 000 € par année, équipe à Montréal. " * 500


def _answering(f, asked: list):
    async def answer(task_id, shape, specs):
        asked.extend(specs)
        return [
            f.make_batch_result(
                task_id,
                spec,
                model="test-model",
                text=json.dumps(
                    {"has_comp": True, "comp_min": 120000, "period": "yearly", "currency": "EUR"}
                ),
            )
            for spec in specs
        ], SimpleNamespace(model="test-model")

    return answer


async def _run(f) -> None:
    db.execute("UPDATE tasks SET status = 'done' WHERE status <> 'done'")
    await comp.PAY.handle(f.make_task("extract_comp", status="running"), {})


@pytest.mark.asyncio
async def test_the_sweep_records_the_hash_of_what_was_read(f, monkeypatch):
    _, url = f.make_ready_job(content=CONTENT)
    asked: list = []
    monkeypatch.setattr("tasks.derive.run_batched", _answering(f, asked))
    await _run(f)

    row = db.query_one(
        "SELECT comp_min, comp_currency, model, content_hash FROM job_comp WHERE url = %s", (url,)
    )
    assert row == {
        "comp_min": 120000,
        "comp_currency": "EUR",
        "model": "test-model",
        "content_hash": hashlib.sha256(CONTENT[:COMP_INPUT_CHARS].encode("utf-8")).hexdigest(),
    }


@pytest.mark.asyncio
async def test_an_unchanged_refetch_is_restamped_and_a_changed_one_is_read_again(f, monkeypatch):
    _, url = f.make_ready_job(content=CONTENT)
    asked: list = []
    monkeypatch.setattr("tasks.derive.run_batched", _answering(f, asked))
    await _run(f)
    assert len(asked) == 1

    # Same text up to the cut: the answer is kept and points at the new fetch.
    fetched = f.make_fetch(url, content=CONTENT + "a footer the model never read")
    await _run(f)
    assert len(asked) == 1
    stored = db.query_one("SELECT content_row_id FROM job_comp WHERE url = %s", (url,))
    assert stored["content_row_id"] == fetched

    f.make_fetch(url, content="Salary USD 90,000 a year. " * 40)
    await _run(f)
    assert len(asked) == 2
