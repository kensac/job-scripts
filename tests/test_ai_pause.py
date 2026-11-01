"""The site-wide AI switch (api.ai_pause): no paid request while it is on, the
non-AI schedule unchanged, and on resume only the pause's last days are read."""

from __future__ import annotations

import datetime

import pytest

from api import ai, ai_pause, db

AI_KINDS = ("verify_new", "reverify_open", "application_sweep", "classify_mail")


def _pause_history(*changes: tuple[datetime.datetime, bool]) -> None:
    admin = db.query_one("INSERT INTO users (sub) VALUES ('pause-admin') RETURNING id")
    assert admin
    for at, value in changes:
        db.execute(
            "INSERT INTO app_config_changes "
            "(key, old_value, new_value, old_value_present, actor_user_id, created_at) "
            "VALUES ('ai_paused', %s, %s, true, %s, %s)",
            (db.jsonb(not value), db.jsonb(value), admin["id"], at),
        )


@pytest.mark.asyncio
async def test_a_live_call_is_refused_before_the_provider(set_config, monkeypatch):
    set_config("ai_paused", True)

    async def provider(*_args, **_kwargs):
        raise AssertionError("the provider was called while AI is paused")

    monkeypatch.setattr(ai, "_parse", provider)
    cfg = ai.AIConfig(provider="openai", api_key="k", key_source="server", model="gpt-6-luna")
    with pytest.raises(ai_pause.AIPaused):
        await ai.parse(cfg, "instructions", "input", ai.BaseModel)


@pytest.mark.asyncio
async def test_a_batch_is_not_submitted_but_paid_work_is_still_collected(set_config, monkeypatch):
    from tasks.runtime import batching

    set_config("ai_paused", True)

    async def submit(*_args, **_kwargs):
        raise AssertionError("a batch was submitted while AI is paused")

    monkeypatch.setattr("core.batch.submit_batches", submit)
    monkeypatch.setattr(batching, "has_batch_work", lambda _task_id: False)
    with pytest.raises(ai_pause.AIPaused):
        await batching.submit_or_collect(1, [object()], "gpt-6-luna", "low", 100, None)

    async def collect(_task_id, _hook):
        return ["collected"]

    monkeypatch.setattr(batching, "has_batch_work", lambda _task_id: True)
    monkeypatch.setattr(batching, "collect_pending", collect)
    assert await batching.submit_or_collect(1, [], "gpt-6-luna", "low", 100, None) == ["collected"]


def test_an_ai_route_says_it_is_paused(client, user_headers, set_config):
    set_config("ai_paused", True)
    r = client.post("/v1/ai/improve-prompt", headers=user_headers, json={"prompt": "x"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "AI_PAUSED"


def test_the_schedule_enqueues_no_ai_work_while_paused(set_config):
    from api import worker

    set_config("ai_paused", True)
    worker.schedule_ingest_cycle()
    kinds = {r["kind"] for r in db.query("SELECT DISTINCT kind FROM tasks")}
    assert not kinds & set(AI_KINDS)
    assert "fetch_missing_content" in kinds, "the non-AI schedule keeps running"


def test_resuming_reads_only_the_last_days_of_the_pause(f, set_config):
    now = datetime.datetime.now(datetime.UTC)
    set_config("ai_resume_backlog_days", 7)
    _pause_history(
        (now - datetime.timedelta(days=30), True), (now - datetime.timedelta(days=1), False)
    )
    ages = {"before_pause": 40, "early_in_pause": 20, "last_week_of_pause": 3, "after": 0}
    ids = {}
    for name, days in ages.items():
        job_id = f.make_job(url=f"https://jobs.test/{name}")
        db.execute(
            "UPDATE jobs SET created_at = %s WHERE id = %s",
            (now - datetime.timedelta(days=days, hours=1), job_id),
        )
        ids[name] = job_id
    skipped = {
        r["id"]
        for r in db.query(
            f"SELECT j.id FROM jobs j WHERE {ai_pause.SKIPPED.format(job='j')}",
            ai_pause.params(),
        )
    }
    assert skipped == {ids["early_in_pause"]}


def test_a_pause_still_on_skips_nothing_yet(set_config):
    now = datetime.datetime.now(datetime.UTC)
    _pause_history((now - datetime.timedelta(days=30), True))
    assert ai_pause.skip_windows() == []
