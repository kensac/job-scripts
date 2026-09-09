import pytest

from api import budget, db
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
