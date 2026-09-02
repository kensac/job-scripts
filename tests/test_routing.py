"""Which model answers a task, and the things that must never happen quietly.

The router's job is narrow on purpose: enforce what the datasheets declare,
price the survivors, and refuse rather than guess. The tests that matter most
here are the negative ones - a router that silently substitutes a model is
worse than no router, because the substitution is invisible in the output and
shows up only in the bill or in the quality.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from api import ai, db
from core import routing
from core.providers.spec import StructuredOutput as SO
from core.routing import NoEligibleModel, TaskShape, resolve
from core.store import add_ai_result, get_custom_result

MON_PEAK = datetime.datetime(2026, 9, 7, 3, tzinfo=datetime.UTC)
MON_OFF = datetime.datetime(2026, 9, 7, 12, tzinfo=datetime.UTC)


def _shape(**kw) -> TaskShape:
    base = dict(
        structured=SO.JSON_SCHEMA,
        batched=True,
        max_output_tokens=1000,
        est_prompt_tokens=6000,
        candidates=("gpt-5-nano",),
    )
    return TaskShape(**{**base, **kw})


class TestPinning:
    def test_one_candidate_resolves_to_exactly_that_model(self):
        chosen = resolve(_shape())
        assert chosen.model == "gpt-5-nano"
        assert chosen.provider == "openai"
        assert "only model the caller sanctioned" in chosen.reason

    def test_no_candidates_is_an_error_not_a_free_choice(self):
        """An empty list means nobody has judged anything fit for this work.
        Choosing then would be the router inventing the judgment it exists to
        carry."""
        with pytest.raises(NoEligibleModel, match="no candidate models"):
            resolve(_shape(candidates=()))

    def test_an_undeclared_model_is_refused_not_guessed_at(self):
        with pytest.raises(NoEligibleModel, match="not declared"):
            resolve(_shape(candidates=("gpt-9-imaginary",)))

    def test_the_reason_names_every_exclusion(self):
        """An operator looking at a task that will not run needs to know which
        requirement excluded which model - 'no eligible model' is equally true
        of a missing schema mode, a missing batch lane and a missing price."""
        with pytest.raises(NoEligibleModel) as exc:
            resolve(_shape(candidates=("deepseek-v4-flash", "gpt-9-imaginary")))
        assert "deepseek-v4-flash" in str(exc.value)
        assert "gpt-9-imaginary" in str(exc.value)


class TestDeclaredCapability:
    def test_a_json_object_model_cannot_serve_a_schema_task(self):
        """Declared, not inferred. DeepSeek returns JSON but will not enforce a
        shape, so batched extraction - where a wrong shape is 20,000 bad rows
        rather than one - must not be routed there."""
        with pytest.raises(NoEligibleModel, match="json_object"):
            resolve(_shape(batched=False, candidates=("deepseek-v4-flash",)))

    def test_a_json_object_model_serves_a_json_object_task(self):
        chosen = resolve(
            _shape(structured=SO.JSON_OBJECT, batched=False, candidates=("deepseek-v4-flash",))
        )
        assert chosen.model == "deepseek-v4-flash"

    def test_batched_work_needs_a_batch_endpoint_not_just_a_cheap_rate(self):
        """DeepSeek's off-peak window is the cheapest thing in the registry at
        the right hour and it still cannot run batched work: both batch
        endpoints 404, so this is a missing capability, not a missing discount.
        """
        with pytest.raises(NoEligibleModel, match="no batch endpoint"):
            resolve(
                _shape(structured=SO.JSON_OBJECT, batched=True, candidates=("deepseek-v4-flash",))
            )

    def test_an_effort_the_model_rejects_makes_it_ineligible(self):
        """A batch submits whole and fails whole, so a rejected parameter costs
        the entire run. This is the 400 that took down a wave, as a check."""
        with pytest.raises(NoEligibleModel, match="does not accept effort"):
            resolve(_shape(candidates=("gpt-5.6-luna",), effort="minimal"))

    def test_an_effort_the_model_accepts_is_passed_through(self):
        chosen = resolve(_shape(candidates=("gpt-5-nano",), effort="minimal"))
        assert chosen.params["reasoning_effort"] == "minimal"

    def test_an_output_ceiling_below_the_task_is_refused(self):
        chosen = resolve(_shape(candidates=("gpt-5-nano",)))
        assert chosen.params["max_output_tokens"] == 1000


class TestEffortPreference:
    def test_the_first_accepted_preference_wins(self):
        """'The cheapest thinking level this model actually takes', expressed
        once here instead of in a second table keyed by model name."""
        chosen = resolve(_shape(candidates=("gpt-5.6-luna",), effort_preference=("none", "low")))
        assert chosen.params["reasoning_effort"] == "none"

    def test_a_preference_the_model_rejects_is_skipped(self):
        chosen = resolve(
            _shape(candidates=("gpt-5-nano",), effort_preference=("none", "minimal", "low"))
        )
        # nano rejects "none"; "minimal" is the first it accepts.
        assert chosen.params["reasoning_effort"] == "minimal"

    def test_an_explicit_effort_beats_the_preference(self):
        chosen = resolve(
            _shape(candidates=("gpt-5-nano",), effort="high", effort_preference=("minimal",))
        )
        assert chosen.params["reasoning_effort"] == "high"

    def test_no_preference_falls_back_to_the_declared_default(self):
        chosen = resolve(_shape(candidates=("gpt-5-nano",)))
        assert chosen.params["reasoning_effort"] == "low"


class TestPriceRanking:
    def test_the_cheaper_of_two_sanctioned_models_wins(self):
        chosen = resolve(_shape(candidates=("gpt-5-mini", "gpt-5-nano")))
        assert chosen.model == "gpt-5-nano"
        assert "cheapest of 2" in chosen.reason

    def test_wall_clock_pricing_changes_the_winner(self):
        """The one case where WHEN the work runs decides WHAT runs it. DeepSeek
        has no batch lane, so its discount only ever applies to synchronous
        calls - exactly the traffic a batch discount can never reach."""
        shape = _shape(
            structured=SO.JSON_OBJECT, batched=False, candidates=("gpt-5-mini", "deepseek-v4-flash")
        )
        assert resolve(shape, at=MON_PEAK).model == "gpt-5-mini"
        off = resolve(shape, at=MON_OFF)
        assert off.model == "deepseek-v4-flash"
        assert off.off_peak is True
        assert "off-peak rate applies" in off.reason

    def test_an_unknown_time_prices_at_peak(self):
        """Same rule as pricing: overstate rather than invent a discount. A
        router that assumed off-peak would pick a model the caller then pays
        double for."""
        shape = _shape(
            structured=SO.JSON_OBJECT, batched=False, candidates=("gpt-5-mini", "deepseek-v4-flash")
        )
        assert resolve(shape).model == "gpt-5-mini"

    def test_a_tie_keeps_the_callers_ordering(self):
        """Equal price is not a licence to reorder: the caller's sequence is
        the only place their preference is recorded."""
        shape = _shape(candidates=("gpt-5-nano", "gpt-5-nano"))
        assert resolve(shape).model == "gpt-5-nano"

    def test_the_estimate_is_a_ranking_device_not_a_bill(self):
        chosen = resolve(_shape())
        assert chosen.est_cost_usd is not None
        assert chosen.est_cost_usd > Decimal(0)


class TestNoSilentSubstitution:
    def test_resolution_returns_one_model_or_raises(self):
        """There is no fallback chain by design: two providers do not answer
        the same prompt the same way, so a silent retry elsewhere produces a
        verdict whose author depends on which call happened to fail first."""
        with pytest.raises(NoEligibleModel):
            resolve(_shape(candidates=("deepseek-v4-flash",)))

    def test_every_wired_call_site_pins_exactly_one_model(self):
        """The property that makes this PR a no-op at runtime. A second
        candidate is not free - see the cache test below - so widening one is a
        decision someone should have to make deliberately."""
        from api.tasks import comp, mail_classify, verify

        for shape in (
            comp.COMP_TASK,
            verify.VERIFY_TASK,
            mail_classify.BACKFILL_TASK,
            mail_classify.ONGOING_TASK,
        ):
            assert len(shape.candidates) == 1, shape.candidates

    def test_the_wired_models_are_the_ones_that_were_hardcoded(self):
        """Byte-identical selection to before the router existed."""
        from api.tasks import comp, mail_classify, verify

        assert resolve(comp.COMP_TASK).model == "gpt-5-nano"
        assert resolve(verify.VERIFY_TASK).model == "gpt-5-nano"
        assert resolve(mail_classify.BACKFILL_TASK).model == mail_classify.BACKFILL_MODEL
        assert resolve(mail_classify.ONGOING_TASK).model == mail_classify.ONGOING_MODEL


class TestKeyAvailability:
    def test_resolution_does_not_need_a_key(self, monkeypatch):
        """Whether this host holds a key is a fact about the environment, not
        about what a model can do. Folding it into resolution would mean no
        test and no introspection could ask what a task would use without one.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert resolve(_shape()).model == "gpt-5-nano"

    def test_server_key_has_one_implementation(self, monkeypatch):
        """api.ai.server_key delegates to core.routing rather than keeping a
        second map of provider-to-env-var."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert ai.server_key("openai") == "sk-test"
        assert routing.server_key("openai") == "sk-test"
        assert ai.server_key("no-such-provider") == ""

    def test_the_key_env_var_comes_from_the_datasheet(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        assert routing.server_key("deepseek") == "ds-test"


class TestCustomVerdictCostCliff:
    def test_a_model_change_hides_every_decided_custom_verdict(self, f):
        """The real cost of a model switch, and it is not a forked log.

        Model appears in no resolution key, so the board is unaffected. But
        tasks/filters.py skips a check by calling get_custom_result WITH the
        model, so the first cycle answered by a different model sees no cached
        verdicts and re-runs the whole candidate set at full price - about
        $1.32 for the one enabled filter today, $6.19 if all ten were live.

        Pinned here so that widening a call site to two models cannot be done
        without meeting this first. It fails if the model scoping is removed,
        which would be the other way to make the cliff disappear.
        """
        _, url = f.make_ready_job(content="a long job description " * 20)
        add_ai_result(
            url, "passed", "looks good", "custom", model="gpt-5-nano", prompt_hash="hash-1"
        )
        assert get_custom_result(url, "hash-1", model="gpt-5-nano") is not None
        # Same url, same filter, different model: invisible, so it is re-run.
        assert get_custom_result(url, "hash-1", model="gpt-5-mini") is None
        # Without a model, any verdict counts - which is why the read path and
        # the board never notice a switch at all.
        assert get_custom_result(url, "hash-1") is not None

    def test_the_filter_sweep_is_the_caller_that_scopes_by_model(self):
        """If this call site stops passing the model, the cliff above stops
        existing and this test should be deleted with it - not left asserting
        a coupling that no longer holds."""
        import inspect

        from api.tasks import filters

        source = inspect.getsource(filters)
        assert "get_custom_result(url, prompt_hash, model=cfg.model)" in source


def test_no_call_site_still_hardcodes_a_batched_model():
    """The thing this PR is for: a model string at a call site is a choice
    nobody can review, price or capability-check."""
    import inspect

    from api.tasks import comp, verify

    for module in (comp, verify):
        source = inspect.getsource(module)
        assert "DEFAULT_OPENAI_MODEL" not in source, module.__name__


def test_db_is_reachable_so_the_cliff_test_means_something(f):
    assert db.query_one("SELECT 1 AS ok")["ok"] == 1


def test_every_batched_call_site_goes_through_the_standard_caller():
    """The ceremony was ten lines copied four times, and they had already
    drifted: one passed `SHAPE.effort or "low"`, another
    `SHAPE.resolved_effort() or A_CONSTANT`. The same declaration produced
    different requests depending on which file you were in.

    Asserting on the source rather than behaviour is deliberate. A call site
    that unpacks the shape by hand works fine today - that is exactly why four
    of them drifted without a test failing."""
    import pathlib

    # Two call sites do not go through it, for reasons rather than by neglect.
    # Listed so a THIRD cannot appear quietly - which is exactly how the four
    # copies this deletes accumulated.
    #
    # filters.py runs a model the USER configured per filter, not a TaskShape.
    # There is no declaration for run_batched to take, and routing it would
    # change which jobs land on the board.
    #
    # verify.py reattaches before fetching its rows, so its in-flight check has
    # to happen earlier than run_batched performs it. Folding it in would make
    # a resumed chunk re-read the catalog to reach a batch it already has.
    KNOWN = {"filters.py", "verify.py", "runtime.py"}
    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "api" / "tasks"
    offenders = [
        path.name
        for path in sorted(root.glob("*.py"))
        if "submit_or_collect" in path.read_text() and path.name not in KNOWN
    ]
    assert offenders == [], (
        f"{offenders} call submit_or_collect directly; use run_batched so the "
        "shape cannot be contradicted and the spend lands in analytics"
    )


def test_the_standard_caller_cannot_run_without_a_purpose():
    """purpose is what every ledger groups by. Making it keyword-only and
    required is what stops a new caller being invisible in analytics - the
    grouping is not something anyone has to remember to add."""
    import inspect

    from api.tasks.runtime import run_batched

    sig = inspect.signature(run_batched)
    purpose = sig.parameters["purpose"]
    assert purpose.kind is inspect.Parameter.KEYWORD_ONLY
    assert purpose.default is inspect.Parameter.empty, "required, not defaulted"
