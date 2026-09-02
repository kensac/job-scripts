"""Configuring which model runs a task, and refusing to let it happen blind.

The screen this feeds exists so the owner of the system can overrule a call
site. The tests are mostly about what he must be shown first, and about the one
thing he must not be able to do: pick a model that cannot run the work, which
would fail mid-batch after the money was spent.
"""

from __future__ import annotations

import pytest

from api import db
from api.tasks import SHAPES


def _get(client, headers, purpose="requirements"):
    r = client.get(f"/v1/admin/task-models/{purpose}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _put(client, headers, purpose, **body):
    return client.put(f"/v1/admin/task-models/{purpose}", headers=headers, json=body)


class TestAccess:
    def test_a_non_admin_cannot_read_it(self, client, user_headers):
        assert client.get("/v1/admin/task-models", headers=user_headers).status_code == 403

    def test_a_non_admin_cannot_change_it(self, client, user_headers):
        r = _put(client, user_headers, "requirements", model="gpt-5-nano")
        assert r.status_code == 403

    def test_an_unknown_task_is_a_404(self, client, admin_headers):
        assert client.get("/v1/admin/task-models/nope", headers=admin_headers).status_code == 404


class TestEligibility:
    def test_every_declared_model_is_returned_with_a_reason(self, client, admin_headers):
        """Ineligible models are sent WITH why, not hidden. A short list with
        the impossible options silently removed is how someone concludes the
        missing model is a bug."""
        body = _get(client, admin_headers)
        by_model = {c["model"]: c for c in body["candidates"]}
        assert by_model["gpt-5-mini"]["eligible"] is True
        assert by_model["deepseek-v4-flash"]["eligible"] is False
        assert "json_object" in by_model["deepseek-v4-flash"]["rejection"]
        assert by_model["claude-opus-5"]["rejection"] == "provider has no batch endpoint"

    def test_eligibility_is_computed_here_not_by_the_client(self, client, admin_headers):
        """Every candidate carries a server-computed verdict, so the UI's idea
        of eligible and the router's cannot drift."""
        for c in _get(client, admin_headers)["candidates"]:
            assert isinstance(c["eligible"], bool)
            assert c["eligible"] == (c["rejection"] == "")

    def test_the_sanctioned_set_is_its_own_field(self, client, admin_headers):
        """So the client never infers 'this is an override' by comparing
        strings - that is how two notions of override drift apart."""
        body = _get(client, admin_headers)
        assert body["sanctioned"] == ["gpt-5-mini"]
        assert body["override_is_outside_sanctioned"] is False


class TestEvidence:
    def test_the_measurement_travels_as_data_with_its_sample_size(self, client, admin_headers):
        """'nano fabricated clearances' and 'nano fabricated clearances in 12
        of 55 postings' are different claims; only the second can be argued
        with. The n is not garnish."""
        body = _get(client, admin_headers)
        nano = next(c for c in body["candidates"] if c["model"] == "gpt-5-nano")
        assert len(nano["evidence"]) == 1
        finding = nano["evidence"][0]
        assert finding["verdict"] == "excluded"
        assert finding["sample_size"] == 60
        assert "12 of 55" in finding["finding"]
        assert finding["measured_on"] == "2026-09-02"

    def test_evidence_is_attached_to_the_model_it_is_about(self, client, admin_headers):
        body = _get(client, admin_headers)
        for c in body["candidates"]:
            for e in c["evidence"]:
                assert e["finding"]
                assert e["sample_size"] > 0
        chosen = next(c for c in body["candidates"] if c["model"] == "gpt-5-mini")
        assert chosen["evidence"][0]["verdict"] == "chosen"

    def test_a_task_with_no_measurement_says_so_with_an_empty_list(self, client, admin_headers):
        body = _get(client, admin_headers, "comp")
        assert all(c["evidence"] == [] for c in body["candidates"])
        assert body["notes"]


class TestCost:
    def test_the_cycle_cost_is_served_not_left_to_the_client(self, client, admin_headers):
        """This repo's rule is that cost comes from the server or nowhere."""
        body = _get(client, admin_headers)
        mini = next(c for c in body["candidates"] if c["model"] == "gpt-5-mini")
        assert mini["est_cost_usd"] is not None
        assert mini["est_cycle_cost_usd"] is not None
        assert float(mini["est_cycle_cost_usd"]) > float(mini["est_cost_usd"])

    def test_the_basis_travels_with_the_number(self, client, admin_headers):
        """So it renders as an estimate with its inputs rather than a price."""
        basis = _get(client, admin_headers)["cost_basis"]
        assert basis["per_cycle"] == SHAPES["requirements"].per_cycle
        assert basis["est_prompt_tokens"] > 0
        assert basis["batched"] is True

    def test_the_delta_is_against_what_runs_today(self, client, admin_headers):
        body = _get(client, admin_headers)
        current = next(c for c in body["candidates"] if c["model"] == "gpt-5-mini")
        assert current["est_cycle_cost_delta_usd"] is not None
        assert float(current["est_cycle_cost_delta_usd"]) == 0
        cheaper = next(c for c in body["candidates"] if c["model"] == "gpt-5-nano")
        assert float(cheaper["est_cycle_cost_delta_usd"]) < 0

    def test_an_ineligible_model_carries_no_price(self, client, admin_headers):
        """Absent means absent. A zero would read as free."""
        body = _get(client, admin_headers)
        blocked = next(c for c in body["candidates"] if not c["eligible"])
        assert blocked["est_cost_usd"] is None
        assert blocked["est_cycle_cost_usd"] is None


class TestOverride:
    def test_a_sanctioned_model_is_not_an_override(self, client, admin_headers):
        r = _put(client, admin_headers, "requirements", model="gpt-5-mini")
        assert r.status_code == 200
        assert r.json()["override_is_outside_sanctioned"] is False
        assert r.json()["resolved"]["overridden"] is False

    def test_an_unsanctioned_but_capable_model_is_allowed_and_says_so(self, client, admin_headers):
        """He owns the system and may overrule a call site. He may not do it
        without the screen saying that is what happened."""
        r = _put(client, admin_headers, "requirements", model="gpt-5-nano", reason="cheaper")
        assert r.status_code == 200
        body = r.json()
        assert body["override_is_outside_sanctioned"] is True
        assert body["resolved"]["model"] == "gpt-5-nano"
        assert body["resolved"]["overridden"] is True
        assert "outside the models this call site sanctioned" in body["resolved"]["reason"]

    def test_a_model_that_cannot_do_the_work_is_refused(self, client, admin_headers):
        """Capability is hard where sanction is soft: this failure would
        otherwise arrive in the middle of a paid batch."""
        r = _put(client, admin_headers, "requirements", model="deepseek-v4-flash")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "INELIGIBLE_MODEL"
        assert "json_object" in r.json()["detail"]["message"]

    def test_an_undeclared_model_is_refused(self, client, admin_headers):
        r = _put(client, admin_headers, "requirements", model="gpt-9-imaginary")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "UNKNOWN_MODEL"

    def test_clearing_returns_the_task_to_its_call_site(self, client, admin_headers):
        _put(client, admin_headers, "requirements", model="gpt-5-nano")
        r = _put(client, admin_headers, "requirements", model=None)
        assert r.status_code == 200
        assert r.json()["override"] is None
        assert r.json()["resolved"]["model"] == "gpt-5-mini"


class TestHistory:
    def test_every_decision_is_kept_including_the_clearing(self, client, admin_headers):
        """A cleared override is a NULL row, not a deletion. Erasing the fact
        that an override existed is the same information loss as overwriting
        it, and a regression is noticed weeks after the switch that caused it.
        """
        _put(client, admin_headers, "requirements", model="gpt-5-nano", reason="trying it")
        _put(client, admin_headers, "requirements", model=None, reason="reverting")
        changes = client.get(
            "/v1/admin/task-models/requirements/history", headers=admin_headers
        ).json()["changes"]
        assert [c["model"] for c in changes] == [None, "gpt-5-nano"]
        assert [c["reason"] for c in changes] == ["reverting", "trying it"]

    def test_a_change_records_who_made_it(self, client, admin_headers):
        _put(client, admin_headers, "requirements", model="gpt-5-nano")
        change = client.get(
            "/v1/admin/task-models/requirements/history", headers=admin_headers
        ).json()["changes"][0]
        assert change["changed_by_email"]
        assert change["created_at"]

    def test_whether_it_was_an_override_is_recorded_at_the_time(self, client, admin_headers):
        """The sanctioned set lives in code and moves, so a row holding only
        the model could not say later whether it was an override when made."""
        _put(client, admin_headers, "requirements", model="gpt-5-nano")
        row = db.query_one(
            "SELECT overrode_sanctioned FROM task_model_overrides ORDER BY id DESC LIMIT 1"
        )
        assert row is not None and row["overrode_sanctioned"] is True


class TestTakesEffect:
    def test_the_next_sweep_reads_the_override(self, client, admin_headers):
        """run_batched resolves per run, so a change lands on the next sweep.
        A batch already submitted finishes on the model that submitted it -
        the request is with the provider and cannot be recalled."""
        from api.tasks.runtime import configured_model

        assert configured_model("requirements") is None
        _put(client, admin_headers, "requirements", model="gpt-5-nano")
        assert configured_model("requirements") == "gpt-5-nano"

    def test_an_unreadable_override_falls_back_rather_than_stopping_a_sweep(self, monkeypatch):
        from api.tasks import runtime

        def boom(*a, **k):
            raise RuntimeError("database is having a moment")

        monkeypatch.setattr(runtime.db, "query_one", boom)
        assert runtime.configured_model("requirements") is None


@pytest.mark.parametrize("purpose", sorted(SHAPES))
def test_every_registered_task_renders(client, admin_headers, purpose):
    """A task that cannot be displayed is a task nobody can configure."""
    body = _get(client, admin_headers, purpose)
    assert body["label"]
    assert body["candidates"]
    assert body["on_model_change"] in ("mixes", "reruns")
