import pytest

from api import ai, db
from api.model_calls import Payer
from core.store import Page
from tasks import filter_execution


@pytest.mark.asyncio
async def test_identity_neutral_live_adapter_has_no_person_state_effects(f, monkeypatch):
    """The execution seam exposes only caller-owned effects, never a person id."""
    user_id = f.make_user()
    job_id = f.make_job()
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'saved')",
        (user_id, job_id),
    )
    job = db.query_one("SELECT url, company, title FROM jobs WHERE id = %s", (job_id,))
    assert job is not None
    snapshot = filter_execution.FilterSnapshot(
        name="No clearance required",
        prompt="Exclude roles requiring clearance",
        on_ambiguous="pass",
        prompt_hash="immutable-snapshot",
    )
    usage_events = []
    progress_events = []
    completions = []

    async def checked(cfg, candidate, page, received_snapshot, received_hooks):
        assert cfg.model == "gpt-5.6-luna"
        assert candidate == job
        assert page == Page(7, "prepared posting")
        assert received_snapshot is snapshot
        assert received_hooks is hooks
        return {"total_tokens": 9}

    monkeypatch.setattr(
        filter_execution,
        "get_contents",
        lambda urls: dict.fromkeys(urls, Page(7, "prepared posting")),
    )
    monkeypatch.setattr(filter_execution, "check_filter", checked)
    hooks = filter_execution.ExecutionHooks(
        verdict_label="managed:test-board",
        key_source="owner",
        payer=Payer(user_id=1),
        purpose="managed_board",
        record_usage=lambda usage, model, batched: usage_events.append((usage, model, batched)),
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda done, total, label: progress_events.append((done, total, label)),
        complete=lambda: completions.append(True),
    )

    await filter_execution.execute_live(
        f.make_task("run_managed_board"),
        ai.AIConfig("openai", "test-key", "owner", "gpt-5.6-luna"),
        snapshot,
        [job],
        hooks,
    )

    # A live call is booked by run_check with its verdict; the hook counts
    # only collected batch items.
    assert usage_events == []
    assert progress_events == [(1, 1, snapshot.name)]
    assert completions == [True]
    person_state = db.query_one(
        "SELECT status FROM user_jobs WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )
    assert person_state is not None
    assert person_state["status"] == "saved"


@pytest.mark.asyncio
async def test_frozen_content_never_refetches_or_reads_a_later_page(f, monkeypatch):
    seen = []

    async def checked(cfg, candidate, page, snapshot, hooks):
        seen.append(page)
        return None

    async def must_not_refresh(*args, **kwargs):
        raise AssertionError("a frozen run must not fetch a later page")

    monkeypatch.setattr(
        filter_execution, "get_contents", lambda urls: {"https://job": Page(9, "later content")}
    )
    monkeypatch.setattr(filter_execution.verdicts, "refresh_page", must_not_refresh)
    monkeypatch.setattr(filter_execution, "check_filter", checked)
    hooks = filter_execution.ExecutionHooks(
        verdict_label="managed:test",
        key_source="owner",
        payer=Payer(user_id=1),
        purpose="managed_board",
        record_usage=lambda usage, model, batched: None,
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda done, total, label: None,
        complete=lambda: None,
    )
    snapshot = filter_execution.FilterSnapshot("managed", "prompt", "filter", "hash")

    await filter_execution.execute_live(
        f.make_task("run_managed_board"),
        ai.AIConfig("openai", "key", "owner", "gpt-5.6-luna"),
        snapshot,
        [
            {
                "url": "https://job",
                "company": "C",
                "title": "T",
                "content": "frozen",
                "content_query_id": 3,
            }
        ],
        hooks,
    )

    assert seen == [Page(3, "frozen")]
