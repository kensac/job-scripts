from types import SimpleNamespace

import pytest

from api import ai, db
from core.filters import build_custom_input
from core.store import Page
from tasks import filters
from tests.factories import filter_config


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
    payload = {"parent_id": parent, "user_id": uid, "config_id": filter_config(flt), "jobs": [job]}
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
    monkeypatch.setattr(filters.verdicts, "refresh_page", forbidden)
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

    text = "fetched posting " * 30

    async def fetch(url, **kwargs):
        calls.append(url)
        return Page(f.make_fetch(url, content=text), text), None

    async def submit(tid, specs, *args):
        assert len(specs) == 1
        assert "fetched posting" in specs[0].input
        return [
            f.make_batch_result(
                tid,
                specs[0],
                model=cfg.model,
                text='{"should_filter": false, "reason": "matches"}',
                usage={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105},
                error=None,
            )
        ]

    monkeypatch.setattr(filters.verdicts, "refresh_page", fetch)
    monkeypatch.setattr(filters, "submit_or_collect", submit)
    tid = f.make_task("run_filter_batch_chunk", payload, status="running")
    await filters.handle_run_filter_batch_chunk(tid, payload)
    assert calls == [job["url"]]
    assert db.query_one("SELECT count(*) AS n FROM ai_queries WHERE check_type='custom'")["n"] == 1
    [answer] = f.answer_pointers(job["url"])
    fetch = db.query_one("SELECT id FROM page_texts WHERE url = %s", (job["url"],))["id"]
    assert answer["page_fetch_id"] == fetch
    assert answer["rebuilt"] == build_custom_input(job["company"], job["title"], text)
    assert answer["model_call_id"] is not None


@pytest.mark.asyncio
async def test_recent_failed_fetch_is_not_repeated_by_scheduled_chunk(setup, f, monkeypatch):
    _uid, _cfg, _flt, job, _parent, payload = setup
    f.make_fetch(job["url"], status="failed")

    async def forbidden(*args, **kwargs):
        pytest.fail("recent failed fetch or missing content must not make a request")

    monkeypatch.setattr(filters.verdicts, "refresh_page", forbidden)
    monkeypatch.setattr(filters, "submit_or_collect", forbidden)
    tid = f.make_task("run_filter_batch_chunk", payload, status="running")
    await filters.handle_run_filter_batch_chunk(tid, payload)
    progress = db.query_one("SELECT progress FROM tasks WHERE id=%s", (tid,))["progress"]
    assert progress["done"] == 0
    assert "later cycle" in progress["label"]


@pytest.mark.asyncio
async def test_scheduled_receipt_replay_ignores_current_configuration(setup, f, monkeypatch):
    _uid, cfg, _flt, job, _parent, payload = setup
    f.make_fetch(job["url"], content="ready posting " * 30)
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
    f.make_fetch(urls[0], content="old raw")
    f.make_fetch(urls[1], content="raw first")
    f.make_fetch(urls[2], content="nonempty")
    f.make_fetch(urls[2], content="")
    expected = {url: content for url in urls if (content := store.get_content(url)) is not None}
    assert expected == {urls[0]: "old raw", urls[1]: "raw first", urls[2]: "nonempty"}
    newest = {
        row["url"]: row["id"]
        for row in db.query(
            "SELECT DISTINCT ON (url) url, id FROM page_texts WHERE url = ANY(%s) "
            "ORDER BY url, id DESC",
            (urls,),
        )
    }
    assert store.get_contents(urls) == {
        url: store.Page(newest[url], text) for url, text in expected.items()
    }
    assert store.get_contents([]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["run_filter_chunk", "run_filter_batch_chunk"])
async def test_a_persons_own_key_keeps_scheduled_runs_live(setup, f, monkeypatch, kind):
    """A person on their own key was promised no cap and their own bill, and
    the batch collector holds only the server's key, so their scheduled sweep
    runs live, billed to them, instead of stopping with BATCH_UNSUPPORTED
    (Kanishk, 2026-09-09)."""
    _uid, cfg, _flt, _job, _parent, payload = setup
    cfg.key_source = "user"
    live_calls = []

    async def live(*args, **kwargs):
        live_calls.append(True)

    async def forbidden(*args, **kwargs):
        pytest.fail("a person's own key never reaches the shared batch collector")

    monkeypatch.setattr(filters, "_process_jobs", live)
    monkeypatch.setattr(filters, "submit_or_collect", forbidden)
    tid = f.make_task(kind, payload, status="running")
    handler = (
        filters.handle_run_filter_chunk
        if kind == "run_filter_chunk"
        else filters.handle_run_filter_batch_chunk
    )
    await handler(tid, payload)
    assert live_calls == [True]
