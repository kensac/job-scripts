"""The spend controls: at the decision, and at the spend.

Two clicks could take a task from $0.51 an hour to $30.80 with nothing to stop
it and nothing to notice. These are the two places that now do.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api import budget, db
from api.tasks import SHAPES


def _put(client, headers, purpose, **body):
    return client.put(f"/v1/admin/task-models/{purpose}", headers=headers, json=body)


def _spend(usd: str, *, fleet: bool = True, user_id=None):
    db.execute(
        "INSERT INTO api_usage (user_id, key_source, purpose, model, prompt_tokens, "
        "completion_tokens, total_tokens, cached_tokens, cost_usd) "
        "VALUES (%s, 'server', 'comp', 'gpt-5-nano', 1, 1, 2, 0, %s)",
        (None if fleet else user_id, usd),
    )


class TestAcknowledgementAtTheDecision:
    def test_a_large_increase_is_refused_until_acknowledged(self, client, admin_headers):
        """comp on gpt-5.6-sol is 60x nano for the same work, hourly."""
        r = _put(client, admin_headers, "comp", model="gpt-5.6-sol")
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "COST_ACKNOWLEDGEMENT_REQUIRED"
        assert float(detail["multiple"]) > 10
        assert detail["current_model"] == "gpt-5-nano"

    def test_the_refusal_says_what_the_numbers_are(self, client, admin_headers):
        """So the person is told before committing rather than discovering it
        when a sweep declines to run."""
        detail = _put(client, admin_headers, "comp", model="gpt-5.6-sol").json()["detail"]
        assert float(detail["new_cycle_cost_usd"]) > float(detail["current_cycle_cost_usd"])
        assert detail["threshold"] == 10

    def test_acknowledging_lets_it_through(self, client, admin_headers):
        """He owns the system and may overrule the control. What he may not do
        is spend 60x by accident."""
        r = _put(client, admin_headers, "comp", model="gpt-5.6-sol", acknowledge_cost=True)
        assert r.status_code == 200
        assert r.json()["resolved"]["model"] == "gpt-5.6-sol"

    def test_the_acknowledgement_is_recorded(self, client, admin_headers):
        _put(client, admin_headers, "comp", model="gpt-5.6-sol", acknowledge_cost=True)
        row = db.query_one(
            "SELECT acknowledged_cost FROM task_model_overrides ORDER BY id DESC LIMIT 1"
        )
        assert row is not None and row["acknowledged_cost"] is True

    def test_a_modest_upgrade_needs_no_acknowledgement(self, client, admin_headers):
        """nano to mini is 5x and is the step this codebase has already taken
        twice on measured evidence. A control that blocked it would be wrong."""
        r = _put(client, admin_headers, "comp", model="gpt-5-mini")
        assert r.status_code == 200

    def test_moving_to_a_cheaper_model_is_never_gated(self, client, admin_headers):
        _put(client, admin_headers, "comp", model="gpt-5.6-sol", acknowledge_cost=True)
        r = _put(client, admin_headers, "comp", model="gpt-5-nano")
        assert r.status_code == 200

    def test_clearing_is_never_gated(self, client, admin_headers):
        _put(client, admin_headers, "comp", model="gpt-5.6-sol", acknowledge_cost=True)
        assert _put(client, admin_headers, "comp", model=None).status_code == 200


class TestCeilingAtTheSpend:
    def test_the_ceiling_is_expressed_in_sweeps_not_dollars(self, monkeypatch):
        """So adding a task or changing a model moves it without anyone
        re-picking a number. Asserted by spending just under and just over."""
        monkeypatch.setattr(budget, "FLEET_WEEKLY_CYCLES", 3)
        ceiling = budget.fleet_cycle_cost_usd() * 3
        _spend(str(ceiling - Decimal("0.01")))
        budget.check_fleet_budget()
        _spend("0.02")
        with pytest.raises(budget.FleetBudgetExceeded):
            budget.check_fleet_budget()

    def test_an_override_is_counted_in_the_ceiling(self, client, admin_headers):
        """A ceiling computed from sanctioned models would be measuring a fleet
        that is not the one running."""
        before = budget.fleet_cycle_cost_usd()
        _put(client, admin_headers, "comp", model="gpt-5.6-sol", acknowledge_cost=True)
        assert budget.fleet_cycle_cost_usd() > before

    def test_spending_under_the_ceiling_is_allowed(self):
        _spend("0.01")
        budget.check_fleet_budget()

    def test_spending_over_the_ceiling_refuses(self, monkeypatch):
        monkeypatch.setattr(budget, "FLEET_WEEKLY_CYCLES", 1)
        _spend(str(budget.fleet_cycle_cost_usd() + Decimal("1")))
        with pytest.raises(budget.FleetBudgetExceeded, match="ceiling"):
            budget.check_fleet_budget()

    def test_the_refusal_names_the_numbers_and_the_way_out(self, monkeypatch):
        monkeypatch.setattr(budget, "FLEET_WEEKLY_CYCLES", 1)
        _spend(str(budget.fleet_cycle_cost_usd() + Decimal("1")))
        with pytest.raises(budget.FleetBudgetExceeded) as exc:
            budget.check_fleet_budget()
        assert "JOBTRACKER_FLEET_WEEKLY_CYCLES" in str(exc.value)

    def test_a_persons_own_spend_is_not_fleet_spend(self, monkeypatch, f):
        """user_id IS NULL is what makes a row fleet work. Counting a user's
        filter run against the fleet ceiling would let one person's usage stop
        every scheduled sweep."""
        monkeypatch.setattr(budget, "FLEET_WEEKLY_CYCLES", 1)
        uid = f.make_user()
        _spend("9999", fleet=False, user_id=uid)
        budget.check_fleet_budget()

    def test_a_zero_ceiling_disables_the_check(self, monkeypatch):
        """How a deliberate backfill runs without editing code."""
        monkeypatch.setattr(budget, "FLEET_WEEKLY_CYCLES", 0)
        _spend("9999")
        budget.check_fleet_budget()

    def test_a_task_that_cannot_run_does_not_break_the_ceiling(self, client, admin_headers):
        """Refusing to compute a ceiling because one task is misconfigured
        would take the control down for every other task."""
        assert budget.fleet_cycle_cost_usd() > 0


class TestTheSweepRefuses:
    def test_run_batched_checks_before_submitting(self):
        """After submission is too late: the provider has the batch and it is
        billable whether or not this system still wants it."""
        import inspect

        from api.tasks.runtime import run_batched

        source = inspect.getsource(run_batched)
        assert "check_fleet_budget()" in source
        assert source.index("check_fleet_budget()") < source.index("submit_or_collect")


def test_every_task_is_covered_by_the_ceiling():
    """A task missing from the registry would spend outside the control."""
    assert set(SHAPES) == {
        "comp",
        "requirements",
        "verify",
        "mail_classify",
        "mail_classify_backfill",
    }
