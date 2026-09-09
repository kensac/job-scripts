from types import SimpleNamespace

import pytest

from api import ai, budget, db
from api.tasks import application, filters
from core import pricing
from core.batch import BatchResult

MODEL = "gpt-5-mini"
RAW_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "total_tokens": 1100,
    "input_tokens_details": {"cached_tokens": 400},
    "output_tokens_details": {"reasoning_tokens": 30},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [True, False], ids=["batch", "live"])
@pytest.mark.parametrize("family", ["filter", "application"])
@pytest.mark.parametrize("outcome", ["success", "empty", "invalid"])
async def test_every_consumed_result_records_transport_and_cached_usage(
    f, monkeypatch, family, outcome, batched
):
    uid = f.make_user()
    cfg = ai.AIConfig(
        provider="openai", api_key="test", key_source="owner" if batched else "byo", model=MODEL
    )
    ent = budget.Entitlement(True, None, 0, False, [])
    job_id = f.make_job()
    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (job_id,))
    f.make_verdict(job["url"], "closed", content="Build useful software.")
    text = (
        '{"should_filter": false, "reason": "fits"}'
        if family == "filter"
        else '{"answer": "Fits."}'
    )
    if outcome == "empty":
        text = None
    elif outcome == "invalid":
        text = "invalid json"

    async def parsed_live(cfg, instructions, input_text, response_model):
        parsed = response_model.model_validate_json(text) if outcome == "success" else None
        return parsed, {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "cached_tokens": 400,
            "reasoning_tokens": 30,
        }

    monkeypatch.setattr(ai, "parse", parsed_live)
    result = BatchResult("result", text=text, usage=RAW_USAGE, batch_id="batch-test")
    if family == "filter":
        flt = f.make_filter(uid, on_ambiguous="keep")
        payload = {"user_id": uid, "filter": flt, "jobs": [job], "parent_id": None}
        task_id = f.make_task("run_filter_batch_chunk", payload, status="running")
        monkeypatch.setattr(filters, "load_config", lambda *args: (ent, cfg))

        async def submitted(*args):
            return {job["url"]: result}

        monkeypatch.setattr(filters, "submit_or_collect", submitted)
        if batched:
            await filters.handle_run_filter_batch_chunk(task_id, payload)
        else:
            await filters._process_jobs(task_id, uid, ent, cfg, flt, [job])
    else:
        db.execute(
            "INSERT INTO user_resumes (user_id, name, text) VALUES (%s, 'main', 'Python')", (uid,)
        )
        application.ensure_answer_rows(uid, job_id, [{"key": "why", "label": "Why us?"}])
        task_id = f.make_task("application_draft", {"user_id": uid}, status="running")
        monkeypatch.setattr(application, "load_config", lambda *args: (ent, cfg))

        async def drafted(*args, **kwargs):
            return {f"{job_id}|why": result}, SimpleNamespace(model=MODEL)

        monkeypatch.setattr(application, "run_batched", drafted)
        await application.draft_rows(
            task_id, uid, [{**job, "job_id": job_id, "key": "why", "question": "Why us?"}]
        )
    ledger = db.query("SELECT * FROM api_usage WHERE user_id = %s", (uid,))
    assert len(ledger) == 1
    row = ledger[0]
    assert row["batched"] is batched
    assert row["cached_tokens"] == 400
    assert row["total_tokens"] == 1100
    assert row["cost_usd"] == pricing.estimate_cost_usd(
        MODEL, 1000, 100, cached_tokens=400, batched=batched
    )
    if family == "filter":
        verdict = db.query_one("SELECT * FROM ai_queries WHERE check_type = 'custom'")
        assert verdict["status"] == ("passed" if outcome == "success" else "failed")
        assert verdict["total_tokens"] == 1100
        assert verdict["cached_tokens"] == 400
        assert verdict["cost_usd"] == row["cost_usd"]
        assert verdict["filter_name"] == f"user{uid}:{flt['name']}"
        assert verdict["instructions"]
        assert verdict["input_content"] == (
            "Company: Acme\nJob Title: Engineer\n\nJob Content:\nBuild useful software."
        )
