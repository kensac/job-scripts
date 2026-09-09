from decimal import Decimal

import pytest

from api import db
from api.tasks import application, filters, runtime
from core import batch, pricing
from core.batch import BatchResult, BatchSpec


def _parked(f, kind, payload, model):
    task_id = f.make_task(kind, {**payload, "batch_ids": ["batch-old"]}, status="running")
    if model == "absent":
        return task_id
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model) VALUES ('batch-old', %s, 'application', %s)",
        (task_id, model),
    )
    return task_id


def _collector(monkeypatch, custom_id, text):
    async def collect(ids, hook):
        assert ids == ["batch-old"]
        hook("batch-old", "completed", {"requests": 1, "completed": 1})
        hook("batch-old", "completed", {"input_tokens": 1000, "output_tokens": 100})
        return {
            custom_id: BatchResult(
                custom_id,
                text=text,
                usage={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                batch_id="batch-old",
            )
        }, []

    monkeypatch.setattr(batch, "collect_finished_batches", collect)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-5-mini", None, "absent"])
async def test_resume_prices_persisted_model_without_resolving_current_configuration(
    f, monkeypatch, model
):
    task_id = _parked(f, "application_draft", {}, model)
    model = None if model == "absent" else model
    _collector(monkeypatch, "answer", '{"answer":"original"}')

    def no_current_configuration(*args, **kwargs):
        raise AssertionError("collection must not consult current model routing")

    monkeypatch.setattr(runtime, "resolve", no_current_configuration)
    results, provenance = await runtime.run_batched(
        task_id,
        application.APPLICATION_TASK,
        [BatchSpec("new", "changed prompt", "changed input", "Draft", {})],
    )
    assert results["answer"].model == model
    assert provenance.model == model
    ledger = db.query("SELECT model, cost_usd FROM api_usage")
    assert len(ledger) == 1
    assert ledger[0]["model"] == model
    expected = pricing.estimate_cost_usd(model, 1000, 100, batched=True)
    assert ledger[0]["cost_usd"] == (
        expected.quantize(Decimal("0.000001")) if expected is not None else None
    )
    assert db.query_one("SELECT count(*) AS n FROM ai_prompts")["n"] == 0


@pytest.mark.asyncio
async def test_filter_collects_paid_results_without_current_key_or_content(f, monkeypatch):
    uid = f.make_user()
    job_id = f.make_job()
    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (job_id,))
    flt = f.make_filter(uid, on_ambiguous="keep")
    payload = {"user_id": uid, "filter": flt, "jobs": [job], "parent_id": None}
    task_id = _parked(f, "run_filter_batch_chunk", payload, "gpt-5-mini")
    _collector(monkeypatch, job["url"], '{"should_filter":false,"reason":"fits"}')

    def no_key(*args):
        raise PermissionError("current credits exhausted")

    monkeypatch.setattr(filters, "load_config", no_key)
    await filters.handle_run_filter_batch_chunk(task_id, payload)
    row = db.query_one(
        "SELECT model, status, input_content FROM ai_queries WHERE check_type = 'custom'"
    )
    assert row == {"model": "gpt-5-mini", "status": "passed", "input_content": None}
    usage = db.query_one("SELECT model, batched FROM api_usage WHERE user_id = %s", (uid,))
    assert usage == {"model": "gpt-5-mini", "batched": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["application_draft", "application_sweep"])
async def test_application_collects_paid_drafts_after_resume_removed_and_auto_draft_disabled(
    f, monkeypatch, kind
):
    uid = f.make_user()
    job_id = f.make_job()
    application.ensure_answer_rows(uid, job_id, [{"key": "why", "label": "Why us?"}])
    db.execute(
        "INSERT INTO user_settings (user_id, prefs) VALUES (%s, %s)",
        (uid, db.jsonb({"auto_draft": False})),
    )
    payload = {"user_id": uid, "job_id": job_id}
    task_id = _parked(f, kind, payload, "gpt-5-mini")
    _collector(monkeypatch, f"{job_id}|why", '{"answer":"Already paid for."}')
    handler = (
        application.handle_application_draft
        if kind == "application_draft"
        else application.handle_application_sweep
    )
    await handler(task_id, payload)
    assert db.query_one("SELECT draft, model FROM application_answers") == {
        "draft": "Already paid for.",
        "model": "gpt-5-mini",
    }
    assert db.query_one("SELECT count(*) AS n FROM api_usage WHERE user_id = %s", (uid,))["n"] == 1
