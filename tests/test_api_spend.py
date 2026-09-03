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


def test_every_ai_caller_appears_in_spend_by_its_purpose(client, admin_headers):
    """The spend page read ai_queries, which is the VERDICT log - URL-keyed,
    and structurally blind to work that is not about a posting. Mail
    classification was $18.49 of real spend writing no verdict row, so the
    largest line item in the system was invisible."""
    from api import budget

    budget.record_fleet_usage("mail_classify", "gpt-5.6-luna", 1_000_000, 100_000)
    budget.record_fleet_usage("comp", "gpt-5-nano", 500_000, 50_000)

    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    purposes = {r["purpose"]: r for r in body["by_purpose"]}
    assert "mail_classify" in purposes, "batched work with no verdict row must still appear"
    assert purposes["mail_classify"]["batched_calls"] == 1
    assert float(purposes["mail_classify"]["cost_usd"]) > 0
    assert body["ledger"]["spend_total_usd"] > 0


def test_a_new_caller_needs_no_wiring_to_show_up(client, admin_headers):
    """The point of the design: grouping is the purpose the hook already
    requires, so a task nobody has thought about yet still reports."""
    from api import budget

    budget.record_fleet_usage("a_purpose_that_did_not_exist", "gpt-5-mini", 1000, 100)
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    assert "a_purpose_that_did_not_exist" in {r["purpose"] for r in body["by_purpose"]}


def test_fleet_work_is_charged_to_nobody(client, admin_headers):
    """Catalog-wide extraction belongs to no user. Attributing it to whichever
    admin is user 1 would make per-user spend a fiction."""
    from api import budget, db

    budget.record_fleet_usage("requirements", "gpt-5-mini", 1000, 100)
    row = db.query_one("SELECT user_id, batched FROM api_usage WHERE purpose = 'requirements'")
    assert row["user_id"] is None
    assert row["batched"] is True


def test_batched_fleet_work_is_priced_at_the_batch_rate(client, admin_headers):
    """A batch is half price. Recording it at the sync rate would overstate the
    largest line item in the system by 2x."""
    from api import budget, db
    from core import pricing

    budget.record_fleet_usage("comp", "gpt-5-nano", 1_000_000, 100_000, batched=True)
    row = db.query_one("SELECT cost_usd FROM api_usage WHERE purpose = 'comp'")
    expected = pricing.estimate_cost_usd("gpt-5-nano", 1_000_000, 100_000, batched=True)
    assert row["cost_usd"] == expected
    assert expected < pricing.estimate_cost_usd("gpt-5-nano", 1_000_000, 100_000, batched=False)


def test_an_unpriced_model_is_counted_but_not_costed(client, admin_headers):
    """None means nobody looked the rate up, never zero. A model we cannot
    price must show as calls with an unpriced count, not as free work."""
    from api import budget, db

    budget.record_fleet_usage("comp", "some-unreleased-model", 1000, 100)
    row = db.query_one("SELECT cost_usd FROM api_usage WHERE model = 'some-unreleased-model'")
    assert row["cost_usd"] is None
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    comp = next(r for r in body["by_purpose"] if r["purpose"] == "comp")
    assert comp["unpriced_calls"] == 1


def test_a_purpose_can_be_opened_to_its_calls(client, admin_headers):
    """by_purpose was a dead end by construction: nothing renders api_usage
    rows, so a purpose's total could be read and never opened. The Responses
    page is over ai_queries, which cannot see work that produced no verdict -
    which is most of the bill."""
    from api import budget

    budget.record_fleet_usage("mail_classify", "gpt-5.6-luna", 1_000_000, 100_000)
    budget.record_fleet_usage("comp", "gpt-5-nano", 1000, 100)

    body = client.get("/v1/admin/spend/calls?purpose=mail_classify", headers=admin_headers).json()
    assert body["totals"]["calls"] == 1
    assert body["calls"][0]["purpose"] == "mail_classify"
    assert body["calls"][0]["batched"] is True


def test_unpriced_is_its_own_question_not_a_cheap_one(client, admin_headers):
    """A NULL cost is not a cheap call. It means nobody looked the rate up, and
    the set we cannot price is a different question from the set that was
    inexpensive."""
    from api import budget

    budget.record_fleet_usage("comp", "some-unreleased-model", 1000, 100)
    budget.record_fleet_usage("comp", "gpt-5-nano", 1000, 100)

    unpriced = client.get(
        "/v1/admin/spend/calls?purpose=comp&unpriced=true", headers=admin_headers
    ).json()
    assert unpriced["totals"]["calls"] == 1
    assert unpriced["calls"][0]["model"] == "some-unreleased-model"
    assert unpriced["calls"][0]["cost_usd"] is None

    priced = client.get(
        "/v1/admin/spend/calls?purpose=comp&unpriced=false", headers=admin_headers
    ).json()
    assert priced["totals"]["calls"] == 1


def test_the_call_list_needs_admin(client, user_headers):
    assert client.get("/v1/admin/spend/calls", headers=user_headers).status_code == 403
