from types import SimpleNamespace

import pytest

from api import ai, db
from api.tasks import filters


@pytest.fixture
def setup(f, monkeypatch):
    uid = f.make_user()
    cfg = ai.AIConfig("openai", "test-key", "owner", "gpt-5-nano")
    monkeypatch.setattr(
        filters, "load_config", lambda *args: (SimpleNamespace(weekly_token_budget=None), cfg)
    )
    flt = f.make_filter(uid)
    job_id = f.make_job()
    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (job_id,))
    parent = f.make_task("run_all_filters", {"user_id": uid, "batched": True}, status="running")
    payload = {"parent_id": parent, "user_id": uid, "filter": flt, "jobs": [job]}
    return uid, cfg, flt, job, parent, payload


@pytest.mark.asyncio
async def test_scheduled_splitter_never_sends_missing_content_live(setup, monkeypatch):
    uid, _cfg, flt, job, parent, _payload = setup
    monkeypatch.setattr(filters, "candidates_for", lambda uid: [job])
    live = []

    async def record_live(*args, **kwargs):
        live.append(True)

    monkeypatch.setattr(filters, "_process_jobs", record_live)
    await filters._run_filters(parent, uid, [flt], batched=True)
    assert not live
    children = db.query(
        "SELECT kind, payload FROM tasks WHERE payload->>'parent_id' = %s", (str(parent),)
    )
    assert len(children) == 1
    assert children[0]["kind"] == "run_filter_batch_chunk"
    assert children[0]["payload"]["scheduled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["run_filter_chunk", "run_filter_batch_chunk"])
@pytest.mark.parametrize(
    ("provider", "model", "key_source"),
    [
        ("openai", "gpt-5-nano", "user"),
        ("openai", "text-embedding-3-small", "owner"),
        ("openai", "unknown-model", "owner"),
        ("xai", "grok-4.3", "owner"),
    ],
)
async def test_legacy_scheduled_children_reject_changed_unsupported_config(
    setup, f, monkeypatch, kind, provider, model, key_source
):
    _uid, cfg, _flt, _job, _parent, payload = setup
    cfg.provider, cfg.model, cfg.key_source = provider, model, key_source

    live_calls = []

    async def no_op(*args, **kwargs):
        live_calls.append(True)

    async def forbidden(*args, **kwargs):
        pytest.fail("Unsupported scheduling must not fetch or submit")

    monkeypatch.setattr(filters, "_process_jobs", no_op)
    monkeypatch.setattr(filters, "submit_or_collect", forbidden)
    monkeypatch.setattr(filters.verdicts, "refresh_content", forbidden)
    tid = f.make_task(kind, payload, status="running")
    handler = (
        filters.handle_run_filter_chunk
        if kind == "run_filter_chunk"
        else filters.handle_run_filter_batch_chunk
    )
    with pytest.raises(RuntimeError, match="BATCH_UNSUPPORTED"):
        await handler(tid, payload)
    assert live_calls == []
    assert (
        "BATCH_UNSUPPORTED"
        in db.query_one("SELECT progress FROM tasks WHERE id=%s", (tid,))["progress"]["label"]
    )


@pytest.mark.asyncio
async def test_scheduled_chunk_fetches_then_batches(setup, f, monkeypatch):
    _uid, cfg, _flt, job, _parent, payload = setup
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        f.make_verdict(url, "content", content="fetched posting " * 30)
        return "fetched posting " * 30, None

    async def submit(tid, specs, *args):
        assert len(specs) == 1
        assert "fetched posting" in specs[0].input
        return [
            f.make_batch_result(
                tid,
                specs[0],
                model=cfg.model,
                text='{"should_filter": false, "reason": "matches"}',
                error=None,
            )
        ]

    monkeypatch.setattr(filters.verdicts, "refresh_content", fetch)
    monkeypatch.setattr(filters, "submit_or_collect", submit)
    tid = f.make_task("run_filter_batch_chunk", payload, status="running")
    await filters.handle_run_filter_batch_chunk(tid, payload)
    assert calls == [job["url"]]
    assert db.query_one("SELECT count(*) AS n FROM ai_queries WHERE check_type='custom'")["n"] == 1


@pytest.mark.asyncio
async def test_recent_failed_fetch_is_not_repeated_by_scheduled_chunk(setup, f, monkeypatch):
    _uid, _cfg, _flt, job, _parent, payload = setup
    f.make_verdict(job["url"], "content", status="failed")

    async def forbidden(*args, **kwargs):
        pytest.fail("recent failed fetch or missing content must not make a request")

    monkeypatch.setattr(filters.verdicts, "refresh_content", forbidden)
    monkeypatch.setattr(filters, "submit_or_collect", forbidden)
    tid = f.make_task("run_filter_batch_chunk", payload, status="running")
    await filters.handle_run_filter_batch_chunk(tid, payload)
    progress = db.query_one("SELECT progress FROM tasks WHERE id=%s", (tid,))["progress"]
    assert progress["done"] == 0
    assert "later cycle" in progress["label"]


@pytest.mark.asyncio
async def test_scheduled_receipt_replay_ignores_current_configuration(setup, f, monkeypatch):
    _uid, cfg, _flt, job, _parent, payload = setup
    f.make_verdict(job["url"], "content", content="ready posting " * 30)
    submissions = []

    async def submit(tid, specs, *args):
        submissions.append(len(specs))
        return [
            f.make_batch_result(
                tid,
                specs[0],
                model=cfg.model,
                text='{"should_filter": false, "reason": "matches"}',
                error=None,
            )
        ]

    monkeypatch.setattr(filters, "submit_or_collect", submit)
    tid = f.make_task("run_filter_batch_chunk", payload, status="running")
    await filters.handle_run_filter_batch_chunk(tid, payload)

    def forbidden(*args):
        pytest.fail("Receipt replay must not re-read current credentials")

    monkeypatch.setattr(filters, "load_config", forbidden)
    await filters.handle_run_filter_batch_chunk(tid, payload)
    assert submissions == [1]
    assert db.query_one("SELECT progress FROM tasks WHERE id=%s", (tid,))["progress"]["done"] == 1


@pytest.mark.asyncio
async def test_interactive_child_keeps_live_execution(setup, f, monkeypatch):
    uid, cfg, _flt, _job, parent, payload = setup
    db.execute("UPDATE tasks SET payload=%s WHERE id=%s", (db.jsonb({"user_id": uid}), parent))
    cfg.key_source = "user"
    called = []

    async def live(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(filters, "_process_jobs", live)
    tid = f.make_task("run_filter_chunk", payload, status="running")
    await filters.handle_run_filter_chunk(tid, payload)
    assert called == [True]


def test_bulk_content_preserves_single_url_raw_content_semantics(f):
    from core import store

    urls = [f"https://content.test/{i}" for i in range(5)]
    f.make_verdict(urls[0], "content", content="old raw")
    f.make_verdict(urls[0], "custom", content="wrapped custom")
    f.make_verdict(urls[1], "content", content="raw first")
    f.make_verdict(urls[1], "closed", content="newer raw copy")
    f.make_verdict(urls[2], "content", content="nonempty")
    f.make_verdict(urls[2], "content", content="")
    f.make_verdict(urls[3], "custom", content="only wrapped")
    expected = {url: content for url in urls if (content := store.get_content(url)) is not None}
    assert expected == {urls[0]: "old raw", urls[1]: "newer raw copy", urls[2]: "nonempty"}
    assert store.get_contents(urls) == expected
    assert store.get_contents([]) == {}
