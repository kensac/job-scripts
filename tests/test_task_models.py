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
from api.tasks.requirements import REQUIREMENTS_MODEL


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
        assert by_model[REQUIREMENTS_MODEL]["eligible"] is True
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
        assert body["sanctioned"] == [REQUIREMENTS_MODEL]
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
        chosen = next(c for c in body["candidates"] if c["model"] == REQUIREMENTS_MODEL)
        assert chosen["evidence"][0]["verdict"] == "chosen"

    def test_a_task_with_no_measurement_says_so_with_an_empty_list(self, client, admin_headers):
        body = _get(client, admin_headers, "comp")
        assert all(c["evidence"] == [] for c in body["candidates"])
        assert body["notes"]


class TestCost:
    def test_the_cycle_cost_is_served_not_left_to_the_client(self, client, admin_headers):
        """This repo's rule is that cost comes from the server or nowhere."""
        body = _get(client, admin_headers)
        mini = next(c for c in body["candidates"] if c["model"] == REQUIREMENTS_MODEL)
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
        current = next(c for c in body["candidates"] if c["model"] == REQUIREMENTS_MODEL)
        assert current["est_cycle_cost_delta_usd"] is not None
        assert float(current["est_cycle_cost_delta_usd"]) == 0
        cheaper = next(c for c in body["candidates"] if c["model"] == "gpt-5-nano")
        assert float(cheaper["est_cycle_cost_delta_usd"]) < 0

    def test_a_time_varying_price_says_so_rather_than_claiming_a_state(self, client, admin_headers):
        """This replaced an `off_peak` flag that could never be true: it was
        computed with no timestamp, which means peak by the pricing rule, so it
        reported "not currently discounted" when what it knew was "nobody asked
        what time it is". False read as a finding.

        What is knowable without a clock is whether the model is billed by the
        hour at all, which is a caveat belonging beside the price.
        """
        body = _get(client, admin_headers, "comp")
        for c in body["candidates"]:
            assert "off_peak" not in c
            assert isinstance(c["price_varies_by_time"], bool)
        # No OpenAI model is billed by the hour; DeepSeek is, and is offered
        # here as an ineligible candidate with its reason.
        assert not any(
            c["price_varies_by_time"] for c in body["candidates"] if c["provider"] == "openai"
        )
        assert all(
            c["price_varies_by_time"] for c in body["candidates"] if c["provider"] == "deepseek"
        )

    def test_an_ineligible_model_carries_no_price(self, client, admin_headers):
        """Absent means absent. A zero would read as free."""
        body = _get(client, admin_headers)
        blocked = next(c for c in body["candidates"] if not c["eligible"])
        assert blocked["est_cost_usd"] is None
        assert blocked["est_cycle_cost_usd"] is None


class TestOneShape:
    def test_list_element_single_get_and_put_are_the_same_object(self, client, admin_headers):
        """One shape, three ways in. A client can refetch the list after a
        change or render the PUT response directly and get identical data -
        which is what makes either integration correct rather than one of them
        being the supported path and the other a trap.

        The list is wrapped as {"tasks": [...]}; the single GET and the PUT
        response are the bare task object, with recent_changes inside it.
        """
        listed = client.get("/v1/admin/task-models", headers=admin_headers).json()
        assert set(listed) == {"tasks"}
        element = next(t for t in listed["tasks"] if t["purpose"] == "comp")
        single = _get(client, admin_headers, "comp")
        put = _put(client, admin_headers, "comp", model="gpt-5-nano").json()
        assert sorted(element) == sorted(single) == sorted(put)
        assert "recent_changes" in single
        assert single["purpose"] == "comp"


class TestOrdering:
    def test_candidates_arrive_in_the_order_they_should_be_shown(self, client, admin_headers):
        """The server orders them because the prices are strings: a client
        sorting those lexicographically puts "9.00" above "10.00", and one
        parsing them into floats to sort has rounded money to avoid it."""
        candidates = _get(client, admin_headers)["candidates"]
        eligible = [c for c in candidates if c["eligible"]]
        assert candidates[: len(eligible)] == eligible, "eligible models come first"
        costs = [c["est_cycle_cost_usd"] for c in eligible]
        assert costs == sorted(costs, key=float), "cheapest eligible first"


class TestEvidenceCannotBeOrphaned:
    def test_every_finding_names_a_model_the_screen_will_show(self):
        """Evidence hangs off a candidate, so a finding about a model that is
        never offered would silently vanish - and it is the warnings that go
        missing, not the reassurances."""
        from core.routing import candidates_for

        for purpose, shape in SHAPES.items():
            shown = {c.model for c in candidates_for(shape)}
            for e in shape.evidence:
                assert e.model in shown, f"{purpose}: evidence about {e.model} is unreachable"


class TestOverride:
    def test_a_sanctioned_model_is_not_an_override(self, client, admin_headers):
        r = _put(client, admin_headers, "requirements", model=REQUIREMENTS_MODEL)
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
        assert r.json()["resolved"]["model"] == REQUIREMENTS_MODEL


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

    @pytest.mark.asyncio
    async def test_the_sweep_sends_the_effort_of_the_model_it_chose(
        self, client, admin_headers, monkeypatch
    ):
        """Under an override the batch carried the sanctioned candidate's
        effort: luna's "none" went to nano and 21,525 lines died on a 400 on
        2026-09-04, then 112 more on 2026-09-05."""
        from api.tasks import runtime
        from api.tasks.requirements import REQUIREMENTS_TASK

        sent = {}

        async def fake_submit(task_id, specs, model, effort, max_tokens, hook):
            sent.update(model=model, effort=effort, max_tokens=max_tokens)
            return {}

        monkeypatch.setattr(runtime, "submit_or_collect", fake_submit)
        _put(client, admin_headers, "requirements", model="gpt-5-nano")
        await runtime.run_batched(1, REQUIREMENTS_TASK, [])
        assert sent["model"] == "gpt-5-nano" and sent["effort"] == "minimal"
        _put(client, admin_headers, "requirements", model=None)
        await runtime.run_batched(1, REQUIREMENTS_TASK, [])
        assert sent["model"] == "gpt-5.6-luna" and sent["effort"] == "none"

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


class TestModelHealth:
    """The screen priced models and said nothing about whether they are doing
    the job, which makes it a price list rather than a decision."""

    def _batch(self, model, *, requests=100, failed=0, completed=True, hours_ago=1):
        db.execute(
            "INSERT INTO ai_batches (provider_batch_id, purpose, model, requests, "
            "completed, failed_count, status, submitted_at, completed_at) "
            "VALUES (%s, 'comp', %s, %s, %s, %s, %s, now() - make_interval(hours => %s), %s)",
            (
                f"b-{model}-{requests}-{failed}-{hours_ago}-{completed}",
                model,
                requests,
                requests - failed,
                failed,
                "completed" if completed else "in_progress",
                hours_ago,
                None,
            ),
        )
        if completed:
            db.execute(
                "UPDATE ai_batches SET completed_at = now() - make_interval(hours => %s) "
                "WHERE provider_batch_id = %s",
                (hours_ago, f"b-{model}-{requests}-{failed}-{hours_ago}-{completed}"),
            )

    def _health(self, client, headers, model, purpose="comp"):
        body = _get(client, headers, purpose)
        return next(c for c in body["candidates"] if c["model"] == model)["health"]

    def test_a_model_that_has_run_nothing_has_no_health_not_a_clean_one(
        self, client, admin_headers
    ):
        """A model nobody has used is not a model with a perfect record, and a
        screen that cannot tell those apart will recommend the untried one."""
        assert self._health(client, admin_headers, "gpt-5-nano") is None

    def test_completions_and_in_flight_are_both_reported(self, client, admin_headers):
        self._batch("gpt-5-nano", hours_ago=2)
        self._batch("gpt-5-nano", completed=False, hours_ago=9)
        h = self._health(client, admin_headers, "gpt-5-nano")
        assert h["in_flight"] == 1
        assert h["last_completed_at"] is not None
        assert h["oldest_in_flight_at"] is not None

    def test_a_stall_is_visible_as_work_outstanding_with_no_completion(self, client, admin_headers):
        """The shape of the live incident: batches submitted, nothing coming
        back. 'in_progress' does not say that; outstanding work with no recent
        completion does."""
        self._batch("gpt-5-mini", completed=False, hours_ago=12)
        h = self._health(client, admin_headers, "gpt-5-mini")
        assert h["in_flight"] == 1
        assert h["last_completed_at"] is None

    def test_failures_are_a_numerator_and_a_denominator(self, client, admin_headers):
        """1 of 3 and 3,000 of 9,000 are different claims; a percentage renders
        them identically."""
        self._batch("gpt-5-mini", requests=1000, failed=16)
        h = self._health(client, admin_headers, "gpt-5-mini")
        assert h["failed_requests"] == 16
        assert h["requests"] == 1000
        assert "rate" not in h and "percent" not in h

    def test_a_model_completing_batches_can_still_be_failing_lines(self, client, admin_headers):
        """The case worth catching: every batch completes and the schema keeps
        breaking inside them."""
        self._batch("gpt-5-mini", requests=500, failed=40, completed=True)
        h = self._health(client, admin_headers, "gpt-5-mini")
        assert h["in_flight"] == 0
        assert h["last_completed_at"] is not None
        assert h["failed_requests"] == 40

    def test_the_window_travels_with_the_numbers(self, client, admin_headers):
        self._batch("gpt-5-nano")
        assert self._health(client, admin_headers, "gpt-5-nano")["window_days"] == 7
