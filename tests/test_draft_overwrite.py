from types import SimpleNamespace

import pytest

from api import ai, budget, db
from api.routers import application as routes
from tasks import application


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
            from api import task_admission
            from api.apply import writes as application_writes

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
        result = f.make_batch_result(
            tid,
            args[2][0],
            text='{"answer":"Stale automatic draft"}',
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            model=cfg.model,
        )
        return [result], SimpleNamespace(model=cfg.model)

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
    from api import task_admission
    from api.apply import writes as application_writes

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
    from api.apply import writes as application_writes

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
    from api import task_admission
    from api.apply import writes as application_writes

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
    from api import task_admission
    from api.apply import writes as application_writes

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


@pytest.mark.asyncio
async def test_sweep_cannot_supersede_pending_refinement(f, monkeypatch):
    from api.apply import writes as application_writes

    uid = f.make_user()
    jid = f.make_job()
    job = db.query_one("SELECT id,url,company,title FROM jobs WHERE id=%s", (jid,))
    db.execute("INSERT INTO user_resumes(user_id,name,text) VALUES (%s,'main','Python')", (uid,))
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    cfg = ai.AIConfig(provider="openai", api_key="test", key_source="owner", model="gpt-5-mini")
    monkeypatch.setattr(routes, "_job", lambda *args: job)
    monkeypatch.setattr(routes.ai_access, "require_config", lambda *args: cfg)

    async def parsed(*args):
        sweep = f.make_task("application_sweep", {"user_id": uid}, status="running")
        assert application_writes.reserve_task(sweep, uid, [{"job_id": jid, "key": "why"}]) == {}
        return application.Draft(answer="Requested refinement"), {}

    monkeypatch.setattr(ai, "parse", parsed)
    row = await routes.refine_answer(
        jid, "why", routes.RefineBody(instruction="Write an answer"), SimpleNamespace(id=uid)
    )
    assert row.draft == "Requested refinement"
    assert [turn["kind"] for turn in row.turns] == ["instruction", "refine"]


@pytest.mark.asyncio
async def test_draft_uses_question_read_under_reservation(f, monkeypatch):
    uid = f.make_user()
    jid = f.make_job()
    job = db.query_one("SELECT id,url,company,title FROM jobs WHERE id=%s", (jid,))
    db.execute("INSERT INTO user_resumes(user_id,name,text) VALUES (%s,'main','Python')", (uid,))
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Current question"}])
    tid = f.make_task("application_draft", {"user_id": uid, "job_id": jid}, status="running")
    cfg = ai.AIConfig(provider="openai", api_key="test", key_source="owner", model="gpt-5-mini")
    monkeypatch.setattr(
        application,
        "load_config",
        lambda *args: (budget.Entitlement(True, None, 0, False, []), cfg),
    )

    async def provider(*args, **kwargs):
        specs = args[2]
        assert "Current question" in specs[0].input
        assert "Old selection question" not in specs[0].input
        result = f.make_batch_result(tid, specs[0], text='{"answer":"Fresh"}', model=cfg.model)
        return [result], SimpleNamespace(model=cfg.model)

    monkeypatch.setattr(application, "run_batched", provider)
    assert (
        await application.draft_rows(
            tid, uid, [{**job, "job_id": jid, "key": "why", "question": "Old selection question"}]
        )
        == 1
    )


def test_recorded_failed_live_result_is_not_submitted_again_on_task_retry(f):
    from api.apply import writes as application_writes

    uid = f.make_user()
    jid = f.make_job()
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    tid = f.make_task("application_draft", {"user_id": uid, "job_id": jid}, status="running")
    rows = [{"job_id": jid, "key": "why"}]
    assert application_writes.reserve_task(tid, uid, rows)
    assert (
        application_writes.record_result(
            tid,
            uid,
            f"{jid}|why",
            None,
            {"total_tokens": 3, "prompt_tokens": 2, "completion_tokens": 1},
            "byo",
            "gpt-5-mini",
            "draft",
            batched=False,
        )
        == 0
    )
    assert application_writes.reserve_task(tid, uid, rows) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["application_draft", "application_sweep"])
async def test_application_receipt_rolls_back_usage_and_answer_until_acknowledged(
    f, monkeypatch, kind
):
    from api.apply import writes as application_writes
    from core.batch import BatchSpec

    uid = f.make_user()
    jid = f.make_job()
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why us?"}])
    payload = {"user_id": uid, "job_id": jid}
    tid = f.make_task(kind, payload, status="running")
    handler = (
        application.handle_application_draft
        if kind == "application_draft"
        else application.handle_application_sweep
    )
    key = f"{jid}|why"
    request = application_writes.reserve_task(tid, uid, [{"job_id": jid, "key": "why"}])[key]
    f.make_batch_result(
        tid,
        BatchSpec(key, "original instructions", "original input", "Draft", {}, context=request),
        text='{"answer":"Paid answer"}',
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        model="gpt-5-mini",
    )
    execute = db.execute

    def fail_ack(sql, params=None):
        if "UPDATE batch_result_receipts SET outcome" in sql:
            raise RuntimeError("ack failed")
        return execute(sql, params)

    monkeypatch.setattr(db, "execute", fail_ack)
    with pytest.raises(RuntimeError, match="ack failed"):
        await handler(tid, payload)
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 0
    assert db.query_one("SELECT draft FROM application_answers")["draft"] is None
    assert db.query_one("SELECT consumed_at FROM batch_result_receipts")["consumed_at"] is None
    monkeypatch.setattr(db, "execute", execute)
    for _ in range(2):
        await handler(tid, payload)
    row = db.query_one("SELECT draft,turns FROM application_answers")
    assert row["draft"] == "Paid answer" and len(row["turns"]) == 1
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 1
    assert db.query_one("SELECT outcome FROM batch_result_receipts")["outcome"] == "written"
    assert "1 written" in application_writes.outcome_note(tid)

    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (tid,))["progress"]
    assert progress["done"] == progress["total"] == 1
