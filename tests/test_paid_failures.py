from types import SimpleNamespace

import pytest

from api import budget, db
from api.tasks import embeddings
from tests.test_provider_deepseek import TestJsonObjectPath as _JsonObjectPath
from tests.test_provider_deepseek import _FakeCompletions


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,content", [("length", '{"ok": tr'), ("stop", '{"wrong":1}')])
async def test_paid_parse_failure_preserves_usage(f, finish, content):
    uid = f.make_user()
    with pytest.raises(ValueError) as error:
        await _JsonObjectPath()._run(_FakeCompletions(content, finish_reason=finish))
    assert error.value.usage["total_tokens"] == 10
    with (
        pytest.raises(ValueError),
        budget.record_parse_failures(uid, "owner", "filter", "deepseek-v4-flash"),
    ):
        raise error.value
    assert db.query_one("SELECT total_tokens FROM api_usage")["total_tokens"] == 10


@pytest.mark.asyncio
async def test_embedding_usage_recorded_before_vector_validation(f, monkeypatch):
    f.make_ready_job(content="a posting with enough detail " * 30)
    task_id = f.make_task("embed_postings", {})
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    async def create(**kwargs):
        return SimpleNamespace(data=[], usage=SimpleNamespace(total_tokens=101, prompt_tokens=101))

    monkeypatch.setattr(
        "openai.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(embeddings=SimpleNamespace(create=create)),
    )
    await embeddings.handle_embed_postings(task_id, {})
    row = db.query_one("SELECT user_id,purpose,total_tokens,batched,cost_usd FROM api_usage")
    assert row is not None
    assert (row["user_id"], row["purpose"], row["total_tokens"], row["batched"]) == (
        None,
        "embedding",
        101,
        False,
    )
    assert row["cost_usd"] > 0
    assert db.query_one("SELECT count(*) AS n FROM job_embeddings")["n"] == 0
