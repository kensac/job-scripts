from decimal import Decimal
from types import SimpleNamespace

import pytest

from api import db
from core.batch import BatchSpec
from tasks import verify
from tests.factories import make_batch_result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["verify_new", "reverify_chunk"])
async def test_joint_verification_allocates_cost_once_and_survives_replay(f, monkeypatch, kind):
    task = f.make_task(kind, {"batch_ids": ["joint-cost"]})
    url = "https://example.test/joint-cost"
    result = make_batch_result(
        task,
        BatchSpec(
            url,
            "verify",
            "posting",
            "VerifyVerdict",
            {},
            context={
                "company": "Company",
                "title": "Engineer",
                "needs_closed": True,
                "needs_clearance": True,
            },
        ),
        text='{"is_closed": false, "closed_reason": "open", '
        '"requires_clearance_or_restrictions": false, "clearance_reason": "none"}',
        model="gpt-5-nano",
        usage={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        batch_id="joint-cost",
    )

    async def collect(*args):
        return [result], SimpleNamespace(model="gpt-5-nano")

    monkeypatch.setattr(verify, "run_batched", collect)
    for _ in range(2):
        if kind == "verify_new":
            await verify.handle_verify_new(task, {})
        else:
            verify._record_reverify_results(task, [result])
    rows = db.query(
        "SELECT check_type, cost_usd, total_tokens FROM ai_queries WHERE url=%s ORDER BY id",
        (url,),
    )
    assert rows == [
        {"check_type": "closed", "cost_usd": Decimal("0.000125"), "total_tokens": 1500},
        {"check_type": "clearance", "cost_usd": Decimal("0"), "total_tokens": 0},
    ]


def test_missing_usage_is_not_a_shared_call(f):
    from api.ai.verdicts import record_ai_verdict

    query = record_ai_verdict(
        url="https://example.test/no-receipt",
        check_type="closed",
        rejected=False,
        reason="open",
        parsed_json="{}",
        usage={},
        model="gpt-5-nano",
    )
    assert db.query_one("SELECT cost_usd FROM ai_queries WHERE id=%s", (query,))["cost_usd"] is None
