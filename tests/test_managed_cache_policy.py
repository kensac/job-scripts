import pytest

from api import ai, db
from api.ai.batch_results import snapshot_specs
from api.model_calls import Payer
from core import batch
from core.answers import FilterDecision
from core.store import Page
from tasks.filtering import execution as filter_execution


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"])
def test_explicit_cache_policy_preserves_request_and_frozen_snapshot(f, model):
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
    after = batch._build_line(replay, model, "medium", 6000)["body"]
    # no_cache: explicit mode and no breakpoint anywhere, so nothing is read or written.
    assert after["prompt_cache_options"] == {"mode": "explicit"}
    assert (after["instructions"], after["input"]) == ("criteria", "posting")
    with pytest.raises(ValueError, match="unsupported prompt cache policy"):
        batch._build_line(replay, "gpt-5-nano", "low", 6000)


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-6-luna"])
def test_only_the_instructions_are_written_to_the_cache(model):
    spec = batch.structured_response_spec("job", "criteria", "posting", FilterDecision)
    body = batch._build_line(spec, model, "medium", 6000)["body"]
    assert "instructions" not in body
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    developer, user = body["input"]
    assert developer == {
        "role": "developer",
        "content": [
            {
                "type": "input_text",
                "text": "criteria",
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }
        ],
    }
    assert user == {"role": "user", "content": [{"type": "input_text", "text": "posting"}]}
    # A model without explicit caching keeps the request it always sent.
    plain = batch._build_line(spec, "gpt-5-nano", "low", 6000)["body"]
    assert (plain["instructions"], plain["input"]) == ("criteria", "posting")
    assert "prompt_cache_options" not in plain


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
        payer=Payer(user_id=1),
        purpose="filter",
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
            contents={url: Page(1, "posting")},
            unavailable=0,
            purpose=purpose,
            submit=submit,
        )
    assert len(captured) == 3
    assert captured[0].context["prompt_cache_policy"] == "no_cache"
    assert captured[1].context.get("prompt_cache_policy") is None
    assert captured[2].context.get("prompt_cache_policy") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "sent"),
    [
        ({"reasoning_effort": "high"}, "high"),
        # An Anthropic setting names it `effort`. It was recorded but
        # "medium" was sent.
        ({"effort": "low"}, "low"),
        ({}, "medium"),
    ],
)
async def test_the_recorded_effort_is_the_effort_sent(f, params, sent):
    _, url = f.make_ready_job(content="posting")
    job = {"url": url, "company": "Company", "title": "Software Engineer"}
    hooks = filter_execution.ExecutionHooks(
        verdict_label="filter",
        key_source="owner",
        payer=Payer(user_id=1),
        purpose="filter",
        record_usage=lambda *_: None,
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda *_: None,
        complete=lambda: None,
    )
    calls = []

    async def submit(task_id, specs, model, effort, *_args):
        calls.append((effort, snapshot_specs(task_id, specs)[0].context["reasoning_effort"]))
        return []

    task = f.make_task("run_filter_batch_chunk", {}, status="running")
    await filter_execution.execute_batch(
        task,
        ai.AIConfig("openai", "key", "owner", "gpt-6-luna", params=params),
        filter_execution.FilterSnapshot("filter", "criteria", "filter", "hash"),
        [job],
        hooks,
        contents={url: Page(1, "posting")},
        unavailable=0,
        submit=submit,
    )
    assert calls == [(sent, sent)]
