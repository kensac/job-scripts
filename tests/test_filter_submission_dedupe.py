from types import SimpleNamespace

import pytest

from api import ai, db
from core.store import add_ai_result
from tests.factories import make_task


def _chunk(uid=7, prompt_hash="criteria", status="running"):
    return make_task(
        "run_filter_batch_chunk",
        {
            "user_id": uid,
            "filter": {"prompt_hash": prompt_hash},
            "jobs": [{"url": "https://posting"}],
        },
        status=status,
    )


def _blocked(task):
    from tasks.board import submission_exclusions

    return submission_exclusions(task, 7, ["https://posting"], "criteria", "gpt-6-luna")


def test_decision_that_arrived_after_planning_prevents_submission():
    task = _chunk()
    add_ai_result(
        "https://posting", "passed", None, "custom", prompt_hash="criteria", model="gpt-6-luna"
    )
    assert _blocked(task) == {"https://posting"}


def test_overlapping_chunks_have_one_owner_even_when_newer_runs_first():
    first = _chunk(status="pending")
    second = _chunk()
    assert _blocked(second) == {"https://posting"}
    assert _blocked(first) == set()


def test_completed_owner_is_replaced_by_its_decision():
    first = _chunk()
    second = _chunk()
    add_ai_result(
        "https://posting", "rejected", None, "custom", prompt_hash="criteria", model="gpt-6-luna"
    )
    db.execute("UPDATE tasks SET status='done' WHERE id=%s", (first,))
    assert _blocked(second) == {"https://posting"}


def test_unpaid_failed_owner_does_not_starve_retries():
    _chunk(status="failed")
    assert _blocked(_chunk()) == set()


def test_other_users_revisions_and_models_do_not_supply_a_decision():
    _chunk(uid=8)
    _chunk(prompt_hash="other")
    add_ai_result(
        "https://posting", "passed", None, "custom", prompt_hash="criteria", model="gpt-5.6-luna"
    )
    assert _blocked(_chunk()) == set()


def test_failed_task_with_paid_batches_still_owns_its_work():
    first = _chunk(status="failed")
    db.execute(
        "UPDATE tasks SET payload=payload || %s WHERE id=%s",
        (db.jsonb({"batch_ids": ["paid-batch"]}), first),
    )
    assert _blocked(_chunk()) == {"https://posting"}


@pytest.mark.asyncio
async def test_handler_rechecks_after_content_preparation(monkeypatch):
    from tasks import filters

    task = _chunk()
    payload = db.query_one("SELECT payload FROM tasks WHERE id=%s", (task,))["payload"]
    payload.update(parent_id=None, scheduled=True)
    payload["filter"].update(name="test", prompt="criteria", on_ambiguous="filter")
    cfg = ai.AIConfig("openai", "test", "owner", "gpt-6-luna")
    monkeypatch.setattr(filters, "load_config", lambda *args: (None, cfg))
    monkeypatch.setattr(filters.batch_policy, "transport", lambda *args: "batch")
    completed = []
    monkeypatch.setattr(
        filters,
        "_personal_hooks",
        lambda *args, **kwargs: SimpleNamespace(
            progress=lambda *args: None, complete=lambda: completed.append(True)
        ),
    )

    async def prepare(*args, **kwargs):
        add_ai_result(
            "https://posting", "passed", None, "custom", prompt_hash="criteria", model="gpt-6-luna"
        )
        return {"https://posting": "content"}, 0

    async def forbidden(*args, **kwargs):
        raise AssertionError("already decided work reached the batch executor")

    monkeypatch.setattr(filters, "prepare_content", prepare)
    monkeypatch.setattr(filters, "execute_batch", forbidden)
    await filters.handle_run_filter_batch_chunk(task, payload)
    assert completed == [True]
