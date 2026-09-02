"""The spend endpoint, asserted on values rather than shape.

Cost is money: a test that only checks the keys exist would pass while the
numbers were wrong, which is the failure mode that matters here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core import pricing

NANO = "gpt-5-nano"


@pytest.fixture
def spend_rows(f):
    """Three calls with hand-checkable costs:
    sync   1M in / 1M out          -> 0.45
    batch  1M in / 1M out          -> 0.225  (half price)
    sync   1M in fully cached      -> 0.005  (cached input at a tenth)
    """
    from core.store import add_ai_result

    add_ai_result(
        "https://s.test/a",
        "passed",
        check_type="closed",
        model=NANO,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
    )
    add_ai_result(
        "https://s.test/b",
        "passed",
        check_type="closed",
        model=NANO,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
        batch_id="batch-1",
    )
    add_ai_result(
        "https://s.test/c",
        "passed",
        check_type="custom",
        model=NANO,
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        cached_tokens=1_000_000,
    )


def test_totals_and_batching(client, admin_headers, spend_rows):
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()

    assert Decimal(str(body["totals"]["cost_usd"])) == Decimal("0.68")
    assert body["totals"]["calls"] == 3
    assert body["totals"]["unpriced_calls"] == 0
    assert body["totals"]["cached_tokens"] == 1_000_000

    b = body["batching"]
    assert (b["batched_calls"], b["sync_calls"]) == (1, 2)
    assert Decimal(str(b["batched_cost_usd"])) == Decimal("0.225")
    # Both sync calls are non-interactive, so both count as batchable; the
    # saving is exactly half of what they cost.
    assert b["batchable_sync_calls"] == 2
    assert Decimal(str(b["unrealized_savings_usd"])) == Decimal("0.2275")


def test_unpriced_model_is_counted_not_silently_zeroed(client, admin_headers):
    """A model with no published price must show up as missing coverage. If it
    summed as zero, the headline would understate the bill and look healthy."""
    from core.store import add_ai_result

    add_ai_result(
        "https://s.test/x",
        "passed",
        check_type="closed",
        model="some-new-model",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
    )
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    assert body["totals"]["unpriced_calls"] == 1
    assert Decimal(str(body["totals"]["cost_usd"])) == 0


def test_joint_call_rows_are_surfaced(client, admin_headers):
    """verify_new books one batched call's usage entirely to the closed row so
    it is not counted twice, leaving clearance with zero tokens. That makes
    check_type an invalid cost centre, so the count must be visible rather
    than leaving clearance looking free."""
    from core.store import add_ai_result

    add_ai_result(
        "https://s.test/j",
        "passed",
        check_type="closed",
        model=NANO,
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        batch_id="b2",
    )
    add_ai_result(
        "https://s.test/j",
        "passed",
        check_type="clearance",
        model=NANO,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        batch_id="b2",
    )
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    by_type = {r["check_type"]: r for r in body["by_check_type"]}
    assert by_type["clearance"]["joint_call_rows"] == 1
    assert by_type["closed"]["joint_call_rows"] == 0


def test_superseded_verdicts_are_counted_as_waste(client, admin_headers):
    """Two decided verdicts on the same (url, check_type): the first was paid
    for and then overwritten, because latest-row-wins."""
    from core.store import add_ai_result

    for status in ("passed", "rejected"):
        add_ai_result(
            "https://s.test/dup",
            status,
            check_type="closed",
            model=NANO,
            prompt_tokens=1_000_000,
            completion_tokens=0,
            total_tokens=1_000_000,
        )
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    assert body["waste"]["superseded_verdicts"] == 1
    assert Decimal(str(body["waste"]["superseded_cost_usd"])) == Decimal("0.05")


def test_cost_matches_the_pricing_function(client, admin_headers, spend_rows):
    """The endpoint sums a stored column; the column is written by
    pricing.estimate_cost_usd. Pin them together so a change to either is
    caught here rather than in a chart."""
    expected = (
        pricing.estimate_cost_usd(NANO, 1_000_000, 1_000_000)
        + pricing.estimate_cost_usd(NANO, 1_000_000, 1_000_000, batched=True)
        + pricing.estimate_cost_usd(NANO, 1_000_000, 0, cached_tokens=1_000_000)
    )
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    assert Decimal(str(body["totals"]["cost_usd"])) == expected


def test_requires_admin(client, user_headers):
    assert client.get("/v1/admin/spend", headers=user_headers).status_code == 403
