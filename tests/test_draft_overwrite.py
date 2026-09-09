from types import SimpleNamespace

import pytest

from api import ai, budget, db
from api.routers import application as routes
from api.tasks import application
from core.batch import BatchResult


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening", ["manual_draft", "user_edit", "user_clear"])
async def test_parked_sweep_cannot_overwrite_newer_answer(f, monkeypatch, intervening):
    uid = f.make_user()
    jid = f.make_job()
    job = db.query_one("SELECT id,url,company,title FROM jobs WHERE id=%s", (jid,))
    db.execute("INSERT INTO user_resumes(user_id,name,text) VALUES (%s,'main','Python')", (uid,))
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    tid = f.make_task("application_sweep", {"user_id": uid}, status="running")
    cfg = ai.AIConfig(provider="openai", api_key="test", key_source="owner", model="gpt-5-mini")
    monkeypatch.setattr(
        application,
        "load_config",
        lambda *args: (budget.Entitlement(True, None, 0, False, []), cfg),
    )
    monkeypatch.setattr(routes, "_job", lambda *args: job)
    expected = None if intervening == "user_clear" else "Newer answer"

    async def provider_returns_after_newer_write(*args, **kwargs):
        if intervening == "manual_draft":
            from api import application_writes, task_admission

            manual = task_admission.enqueue(
                "application_draft", {"user_id": uid, "job_id": jid}, {}
            )
            application_writes.record_result(
                manual.task_id,
                uid,
                f"{jid}|why",
                expected,
                {},
                "owner",
                "gpt-5-mini",
                "draft",
                batched=True,
            )
        else:
            routes.put_answer(
                jid, "why", routes.AnswerPut(draft=expected or ""), SimpleNamespace(id=uid)
            )
        return {
            f"{jid}|why": BatchResult(
                "result",
                text='{"answer":"Stale automatic draft"}',
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                batch_id="batch-test",
            )
        }, SimpleNamespace(model=cfg.model)

    monkeypatch.setattr(application, "run_batched", provider_returns_after_newer_write)
    written = await application.draft_rows(
        tid, uid, [{**job, "job_id": jid, "key": "why", "question": "Why us?"}], kind="sweep"
    )
    row = db.query_one(
        "SELECT draft,turns FROM application_answers WHERE user_id=%s AND job_id=%s", (uid, jid)
    )
    assert row["draft"] == expected
    assert written == 0
    assert len(row["turns"]) == 1


def test_manual_admission_supersedes_sweep_before_either_completes(f):
    from api import application_writes, task_admission

    uid = f.make_user()
    jid = f.make_job()
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    sweep = f.make_task("application_sweep", {"user_id": uid}, status="awaiting_batch")
    key = f"{jid}|why"
    assert key in application_writes.reserve_task(sweep, uid, [{"job_id": jid, "key": "why"}])
    manual = task_admission.enqueue("application_draft", {"user_id": uid, "job_id": jid}, {})
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    assert (
        application_writes.record_result(
            sweep, uid, key, "Old", usage, "owner", "gpt-5-mini", "sweep", batched=True
        )
        == 0
    )
    assert (
        application_writes.record_result(
            manual.task_id,
            uid,
            key,
            "Requested",
            usage,
            "owner",
            "gpt-5-mini",
            "draft",
            batched=True,
        )
        == 1
    )
    assert (
        application_writes.record_result(
            manual.task_id,
            uid,
            key,
            "Requested",
            usage,
            "owner",
            "gpt-5-mini",
            "draft",
            batched=True,
        )
        == 0
    )
    row = db.query_one(
        "SELECT draft, turns FROM application_answers WHERE user_id=%s AND job_id=%s", (uid, jid)
    )
    assert row["draft"] == "Requested"
    assert len(row["turns"]) == 1
    assert db.query_one("SELECT count(*) AS n FROM api_usage WHERE user_id=%s", (uid,))["n"] == 2


def test_legacy_result_is_accounted_without_guessing_answer_generation(f):
    from api import application_writes

    uid = f.make_user()
    jid = f.make_job()
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    tid = f.make_task("application_sweep", {"user_id": uid}, status="awaiting_batch")
    key = f"{jid}|why"
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    for _ in range(2):
        assert (
            application_writes.record_result(
                tid, uid, key, "Unverifiable", usage, "owner", "gpt-5-mini", "sweep", batched=True
            )
            == 0
        )
    assert (
        db.query_one("SELECT draft FROM application_answers WHERE user_id=%s", (uid,))["draft"]
        is None
    )
    assert db.query_one("SELECT count(*) AS n FROM api_usage WHERE user_id=%s", (uid,))["n"] == 1
    assert (
        db.query_one("SELECT payload FROM tasks WHERE id=%s", (tid,))["payload"]["draft_results"][
            key
        ]
        == "unknown_request"
    )


@pytest.mark.asyncio
async def test_refinement_preserves_concurrent_edit_and_cached_usage(f, monkeypatch):
    from fastapi import HTTPException

    uid = f.make_user()
    jid = f.make_job()
    job = db.query_one("SELECT id,url,company,title FROM jobs WHERE id=%s", (jid,))
    db.execute("INSERT INTO user_resumes(user_id,name,text) VALUES (%s,'main','Python')", (uid,))
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    cfg = ai.AIConfig(provider="openai", api_key="test", key_source="owner", model="gpt-5-mini")
    user = SimpleNamespace(id=uid)
    monkeypatch.setattr(routes, "_job", lambda *args: job)
    monkeypatch.setattr(routes.ai_access, "require_config", lambda *args: cfg)

    async def parsed(*args):
        routes.put_answer(jid, "why", routes.AnswerPut(draft="Keep my edit"), user)
        return application.Draft(answer="Stale refinement"), {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "cached_tokens": 400,
        }

    monkeypatch.setattr(ai, "parse", parsed)
    with pytest.raises(HTTPException) as caught:
        await routes.refine_answer(jid, "why", routes.RefineBody(instruction="Be concise"), user)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "ANSWER_CHANGED"
    assert (
        db.query_one("SELECT draft FROM application_answers WHERE user_id=%s", (uid,))["draft"]
        == "Keep my edit"
    )
    usage = db.query("SELECT cached_tokens,total_tokens FROM api_usage WHERE user_id=%s", (uid,))
    assert usage == [{"cached_tokens": 400, "total_tokens": 1100}]


def test_manual_refresh_keeps_its_reservation_and_question_change_invalidates_others(f):
    from api import application_writes, task_admission

    uid = f.make_user()
    jid = f.make_job()
    rows = [{"job_id": jid, "key": "why"}]
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Old question"}])
    sweep = f.make_task("application_sweep", {"user_id": uid}, status="awaiting_batch")
    application_writes.reserve_task(sweep, uid, rows)
    manual = task_admission.enqueue(
        "application_draft", {"user_id": uid, "job_id": jid}, {"refresh": True}
    )
    application.ensure_answer_rows(
        uid, jid, [{"key": "why", "label": "Refreshed question"}], task_id=manual.task_id
    )
    assert application_writes.reserve_task(sweep, uid, rows) == {}
    assert application_writes.reserve_task(manual.task_id, uid, rows)
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Changed elsewhere"}])
    assert application_writes.reserve_task(manual.task_id, uid, rows) == {}


def test_sweep_never_reserves_over_pending_manual_request_or_explicit_clear(f, monkeypatch):
    from api import application_writes, task_admission

    uid = f.make_user()
    jid = f.make_job()
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    task_admission.enqueue("application_draft", {"user_id": uid, "job_id": jid}, {})
    sweep = f.make_task("application_sweep", {"user_id": uid}, status="running")
    assert application_writes.reserve_task(sweep, uid, [{"job_id": jid, "key": "why"}]) == {}
    db.execute("UPDATE tasks SET status='done' WHERE kind='application_draft'")
    monkeypatch.setattr(routes, "_job", lambda *args: {"id": jid})
    routes.put_answer(jid, "why", routes.AnswerPut(draft=""), SimpleNamespace(id=uid))
    assert application_writes.reserve_task(sweep, uid, [{"job_id": jid, "key": "why"}]) == {}
