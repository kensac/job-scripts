from contextlib import nullcontext

import pytest

from api import ai, db
from api.ai.batch_results import snapshot_specs
from core import batch
from core.answers import FilterDecision
from tasks import filter_execution


def test_explicit_cache_policy_preserves_request_and_frozen_snapshot(f):
    task = f.make_task("run_managed_board_batch", {}, status="running")
    original = batch.structured_response_spec("job", "criteria", "posting", FilterDecision)
    controlled = batch.structured_response_spec(
        "job",
        "criteria",
        "posting",
        FilterDecision,
        context={"prompt_cache_policy": "no_cache"},
    )
    frozen = snapshot_specs(task, [controlled])[0]
    replay = snapshot_specs(task, [original])[0]
    assert replay.context == frozen.context
    before = batch._build_line(original, "gpt-5.6-luna", "medium", 6000)
    after = batch._build_line(replay, "gpt-5.6-luna", "medium", 6000)
    assert after["body"].pop("prompt_cache_options") == {"mode": "explicit"}
    assert after == before
    with pytest.raises(ValueError, match="unsupported prompt cache policy"):
        batch._build_line(replay, "gpt-5-nano", "low", 6000)


@pytest.mark.asyncio
async def test_only_new_managed_requests_receive_cache_control(f):
    db.execute(
        "INSERT INTO app_config (key,value) VALUES ('managed_board_cache_writes_enabled', 'false') "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
    )
    _, url = f.make_ready_job(content="posting")
    job = {"url": url, "company": "Company", "title": "Software Engineer"}
    snapshot = filter_execution.FilterSnapshot("filter", "criteria", "filter", "hash")
    hooks = filter_execution.ExecutionHooks(
        verdict_label="filter",
        key_source="owner",
        record_failure=lambda _: nullcontext(),
        record_usage=lambda *_: None,
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda *_: None,
        complete=lambda: None,
    )
    captured = []

    async def submit(task_id, specs, *_args):
        captured.extend(snapshot_specs(task_id, specs))
        return []

    for purpose, model in [
        ("managed_board", "gpt-5.6-luna"),
        ("filter", "gpt-5.6-luna"),
        ("managed_board", "gpt-5-nano"),
    ]:
        task = f.make_task("run_managed_board_batch", {}, status="running")
        await filter_execution.execute_batch(
            task,
            ai.AIConfig("openai", "key", "owner", model),
            snapshot,
            [job],
            hooks,
            contents={url: "posting"},
            unavailable=0,
            purpose=purpose,
            submit=submit,
        )
    assert len(captured) == 3
    assert captured[0].context["prompt_cache_policy"] == "no_cache"
    assert captured[1].context.get("prompt_cache_policy") is None
    assert captured[2].context.get("prompt_cache_policy") is None
