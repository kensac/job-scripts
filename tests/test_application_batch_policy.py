import pytest

from api import ai, db
from api.apply import writes as application_writes
from api.board import visibility
from core import batch
from core.batch import BatchSpec
from tasks import application, runtime


def _setup(f, *, cached=True):
    uid = f.make_user(groups=["infra-admins"])
    source = f.make_source()
    f.subscribe(uid, source)
    jid, url = f.make_ready_job(source=source)
    visibility.recompute(uid)
    db.execute(
        "INSERT INTO user_resumes(user_id,name,text) VALUES(%s,'main','Python engineer')", (uid,)
    )
    if cached:
        db.execute(
            "INSERT INTO application_forms(url,questions) VALUES(%s,%s)",
            (url, db.jsonb([{"key": "why", "label": "Why this role?", "kind": "long"}])),
        )
    return uid, jid, url


@pytest.mark.asyncio
async def test_unsupported_shared_transport_is_refused_before_form_reads(f, monkeypatch):
    uid, _, _ = _setup(f, cached=False)
    task_id = f.make_task("application_sweep", {"user_id": uid}, status="running")
    cfg = ai.AIConfig("anthropic", "test", "owner", "claude-test")
    monkeypatch.setattr(application, "load_config", lambda uid: (None, cfg))
    read = []
    monkeypatch.setattr(application, "read_form", read.append)
    with pytest.raises(RuntimeError, match="BATCH_UNSUPPORTED"):
        await application.handle_application_sweep(task_id, {"user_id": uid})
    assert read == []
    assert db.query_one("SELECT count(*) AS n FROM application_answers")["n"] == 0


@pytest.mark.asyncio
async def test_scheduled_draft_gate_precedes_generation_reservation(f, monkeypatch):
    uid, jid, url = _setup(f)
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why?"}])
    task_id = f.make_task("application_sweep", {"user_id": uid}, status="running")
    cfg = ai.AIConfig("anthropic", "test", "owner", "claude-test")
    monkeypatch.setattr(application, "load_config", lambda uid: (None, cfg))
    rows = [
        {
            "job_id": jid,
            "url": url,
            "company": "Example",
            "title": "Role",
            "key": "why",
            "question": "Why?",
        }
    ]
    with pytest.raises(RuntimeError, match="BATCH_UNSUPPORTED"):
        await application.draft_rows(task_id, uid, rows, scheduled=True)
    assert db.query_one("SELECT draft_revision FROM application_answers")["draft_revision"] == 0
    assert (
        "draft_requests"
        not in db.query_one("SELECT payload FROM tasks WHERE id=%s", (task_id,))["payload"]
    )


@pytest.mark.asyncio
async def test_application_shape_is_independent_of_interactive_model(f, monkeypatch):
    uid, _, _ = _setup(f)
    task_id = f.make_task("application_sweep", {"user_id": uid}, status="running")
    cfg = ai.AIConfig("openai", "test", "owner", "interactive-only-model")
    monkeypatch.setattr(application, "load_config", lambda uid: (None, cfg))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    submitted = []

    async def submit(specs, model, effort, max_output, on_event=None):
        submitted.append(model)
        on_event("application-batch", "submitted", {"requests": len(specs)})
        return ["application-batch"]

    monkeypatch.setattr(batch, "submit_responses_batches", submit)
    with pytest.raises(runtime.AwaitingBatch):
        await application.handle_application_sweep(task_id, {"user_id": uid})
    assert len(submitted) == 1 and submitted[0] != cfg.model
    assert db.query_one("SELECT model FROM ai_batches")["model"] == submitted[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_personal_key_live_behavior_is_preserved(f, monkeypatch, scheduled):
    uid, jid, _ = _setup(f)
    kind = "application_sweep" if scheduled else "application_draft"
    payload = {"user_id": uid, "job_id": jid}
    task_id = f.make_task(kind, payload, status="running")
    cfg = ai.AIConfig("openai", "test", "byo", "gpt-5-mini")
    monkeypatch.setattr(application, "load_config", lambda uid: (None, cfg))

    async def parse(*args):
        return application.Draft(answer="Personal answer"), {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }

    async def no_batch(*args, **kwargs):
        raise AssertionError("personal credentials reached shared batch transport")

    monkeypatch.setattr(application.ai, "parse", parse)
    monkeypatch.setattr(application, "run_batched", no_batch)
    handler = (
        application.handle_application_sweep if scheduled else application.handle_application_draft
    )
    await handler(task_id, payload)
    assert db.query_one("SELECT draft FROM application_answers")["draft"] == "Personal answer"
    assert db.query_one("SELECT key_source,batched FROM api_usage") == {
        "key_source": "byo",
        "batched": False,
    }


@pytest.mark.asyncio
async def test_paid_application_batch_collects_before_current_configuration(f, monkeypatch):
    uid, jid, _url = _setup(f)
    application.ensure_answer_rows(uid, jid, [{"key": "why", "label": "Why?"}])
    task_id = f.make_task("application_sweep", {"user_id": uid}, status="running")
    custom_id = f"{jid}|why"
    reserved = application_writes.reserve_task(task_id, uid, [{"job_id": jid, "key": "why"}])
    spec = BatchSpec(custom_id, "rules", "original", "Draft", {}, context=reserved[custom_id])
    f.make_batch_result(
        task_id,
        spec,
        text='{"answer":"Paid answer"}',
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        model="gpt-5-mini",
    )
    db.execute("DELETE FROM user_resumes WHERE user_id=%s", (uid,))

    def no_config(*args):
        raise AssertionError("paid collection reread changed configuration")

    monkeypatch.setattr(application, "load_config", no_config)
    await application.handle_application_sweep(task_id, {"user_id": uid})
    assert db.query_one("SELECT draft FROM application_answers")["draft"] == "Paid answer"
    assert db.query_one("SELECT key_source,batched FROM api_usage") == {
        "key_source": "owner",
        "batched": True,
    }


@pytest.mark.asyncio
async def test_shared_transport_is_rechecked_before_reserving_drafts(f, monkeypatch):
    uid, _, _ = _setup(f)
    task_id = f.make_task("application_sweep", {"user_id": uid}, status="running")
    configs = iter(
        [
            ai.AIConfig("openai", "test", "owner", "gpt-5-mini"),
            ai.AIConfig("anthropic", "test", "owner", "claude-test"),
        ]
    )
    monkeypatch.setattr(application, "load_config", lambda uid: (None, next(configs)))

    async def no_calls(*args, **kwargs):
        raise AssertionError("changed credentials reached provider")

    monkeypatch.setattr(application, "run_batched", no_calls)
    with pytest.raises(RuntimeError, match="BATCH_UNSUPPORTED"):
        await application.handle_application_sweep(task_id, {"user_id": uid})
    assert db.query_one("SELECT draft_revision FROM application_answers")["draft_revision"] == 0
    assert (
        "draft_requests"
        not in db.query_one("SELECT payload FROM tasks WHERE id=%s", (task_id,))["payload"]
    )
