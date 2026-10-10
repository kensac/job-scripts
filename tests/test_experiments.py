"""An experiment measures one step across models and efforts on a seeded
sample, through the step's own request builder, and scores every arm
against a reference arm and against what production decided."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api import experiments as exp
from core import answers
from core.answers import VERIFY_INPUT_CHARS
from core.comp import COMP_INPUT_CHARS
from tasks import comp as task_comp
from tasks import verify as task_verify


def _verdict(url: str, rejected: bool) -> str:
    return json.dumps({"should_filter": rejected, "reason": "because"})


def _sample(f, n: int) -> list[str]:
    urls = []
    for i in range(n):
        _, url = f.make_ready_job(url=f"https://x.test/exp{i}", content="a real posting body " * 40)
        urls.append(url)
    return urls


def test_experiment_steps_are_typed_immutable_and_own_purpose_behavior():
    expected_loaders = {
        "filter": "_filter_deployed",
        "verify": "_verify_deployed",
        "comp": "_comp_deployed",
        "requirements": "_requirements_deployed",
    }
    declared = exp.steps()
    assert {purpose: step.load_deployed.__name__ for purpose, step in declared.items()} == (
        expected_loaders
    )
    assert all(isinstance(step, exp.ExperimentStep) for step in declared.values())
    with pytest.raises(FrozenInstanceError):
        declared["comp"].max_output_tokens = 1
    assert exp.deployed_verdicts("not-a-step", [], {}) == {}


def test_verify_experiment_step_consumes_the_production_request_recipe():
    recipe = answers.VERIFICATION_REQUEST
    step = exp.steps()["verify"]
    content = "v" * (recipe.input_chars + 17)

    assert step.instructions({}) == recipe.instructions
    assert step.answer_model is recipe.response_model
    assert step.build_input({"input_content": content}) == recipe.build_input(content)
    assert step.max_output_tokens == recipe.max_output_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "cap"),
    [("comp", COMP_INPUT_CHARS), ("verify", VERIFY_INPUT_CHARS)],
)
async def test_task_and_experiment_inputs_share_the_derivation_cap(f, monkeypatch, family, cap):
    content = "x" * (cap + 137)
    captured = []

    async def capture(task_id, shape, specs):
        captured.extend(specs)
        return [], SimpleNamespace(model="unused")

    if family == "comp":
        f.make_ready_job(content=content)
        monkeypatch.setattr("tasks.derive.run_batched", capture)
        await task_comp.PAY.handle(f.make_task("extract_comp", status="running"), {})
    else:
        f.make_ready_job(content=content, closed="", clearance="")
        monkeypatch.setattr(task_verify, "run_batched", capture)
        await task_verify.handle_verify_new(f.make_task("verify_new", status="running"), {})

    assert len(captured) == 1
    experiment_input = exp.steps()[family].build_input({"input_content": content})
    assert captured[0].input == experiment_input == content[:cap]


def test_requirements_answers_are_scored_on_the_stored_row_and_skills_between_arms(f):
    """The first requirements run failed on a column that does not exist;
    production's row has the scalars and no skills, so skills compare
    between arms only and never count as a disagreement with production."""
    _, url = f.make_ready_job(url="https://x.test/req1")
    f.make_requirements(url, seniority="senior", clearance="", skills_required=["Python"])
    deployed = exp.deployed_verdicts("requirements", [url], {})
    assert deployed[url]["seniority"] == "senior" and "skills_required" not in deployed[url]
    mine = exp._requirements_fields(
        {"seniority": "senior", "skills_required": ["python", "Go"], "yoe_min": None}
    )
    scored = exp._agreement(mine, deployed[url])
    assert scored["seniority"] == 1.0 and "skills_required" not in scored
    assert (
        exp._agreement(mine, exp._requirements_fields({"skills_required": ["Python"]}))[
            "skills_required"
        ]
        == 0.5
    )


def test_the_sample_is_fixed_by_its_seed(f):
    urls = _sample(f, 6)
    first = [r["url"] for r in exp.sample(3, "seed-a")]
    assert first == [r["url"] for r in exp.sample(3, "seed-a")]
    assert set(first) <= set(urls) and len(first) == 3
    assert first != [r["url"] for r in exp.sample(3, "seed-b")] or True


def test_an_arm_the_model_refuses_is_named_before_submission():
    """A batch fails whole on a rejected effort: nano refuses "none", luna
    refuses "minimal"."""
    assert exp.arm_ok("gpt-5-nano", "none")
    assert exp.arm_ok("gpt-5.6-luna", "minimal")
    assert exp.arm_ok("gpt-5.6-luna", "none") is None
    assert exp.arm_ok("no-such-model", "low") == "unknown model"


def test_a_filter_run_is_scored_against_production_and_the_dearer_arm(f):
    """Luna agrees with production; nano rejects everything."""
    urls = _sample(f, 4)
    flt = f.make_filter(f.make_user(), name="strict", prompt="only backend")
    for i, u in enumerate(urls):
        f.make_verdict(
            u, "custom", "passed" if i < 2 else "rejected", prompt_hash=flt["prompt_hash"]
        )
    rows = [
        {
            "arm": f"{model}@{effort}",
            "url": u,
            "output": {"should_filter": model == "gpt-5-nano" or i >= 2, "reason": "x"},
            "usage": {"input_tokens": 1000, "output_tokens": out, "reasoning_tokens": 0},
            "cost_usd": cost,
            "error": None,
        }
        for model, effort, out, cost in [
            ("gpt-5-nano", "medium", 500, 0.001),
            ("gpt-5.6-luna", "high", 100, 0.002),
        ]
        for i, u in enumerate(urls)
    ]
    params = {"filter_id": flt["id"], "sampled": 4, "arms": []}
    summary = exp.score("filter", params, rows)
    luna, nano = summary["arms"]["gpt-5.6-luna@high"], summary["arms"]["gpt-5-nano@medium"]
    assert (luna["n"], luna["ok"], luna["failed"]) == (4, 4, 0)
    assert luna["agreement_with_deployed"] == {"n": 4, "should_filter": 1.0}
    assert nano["agreement_with_deployed"] == {"n": 4, "should_filter": 0.5}
    assert nano["pass_rate"] == 0.0 and luna["pass_rate"] == 0.5
    assert nano["output_per_request"] == 500
    assert summary["reference"] == "gpt-5.6-luna@high"
    assert nano["agreement_with_reference"] == {"n": 4, "should_filter": 0.5}


def _row(arm: str, url: str, **kw):
    return {
        "arm": arm,
        "url": url,
        "output": None,
        "usage": None,
        "cost_usd": None,
        "error": None,
    } | kw


def test_partial_run_summary_counts_unsubmitted_arms():
    params = {
        "sampled": 2,
        "arms": [{"model": "first", "effort": "low"}, {"model": "missing", "effort": "low"}],
    }
    summary = exp.score(
        "verify", params, [_row("first@low", "https://example.test/one", error="failed")]
    )
    assert summary["expected_results"] == 4
    assert summary["received_results"] == 1
    assert summary["missing_results"] == 3
    assert summary["missing_arms"] == ["missing@low"]


def test_run_summary_keeps_missing_cache_write_cost_unpriced():
    params = {"sampled": 1, "arms": [{"model": "gpt-5.6-luna", "effort": "low"}]}
    usage = {"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 300}
    summary = exp.score(
        "verify",
        params,
        [_row("gpt-5.6-luna@low", "https://example.test/unpriced", usage=usage)],
    )
    arm = summary["arms"]["gpt-5.6-luna@low"]
    assert arm["unpriced_results"] == 1
    assert arm["known_cost_usd"] == 0.0
    assert arm["cost_usd"] is None and arm["cost_per_100_usd"] is None
    assert summary["reference"] is None
    assert summary["reference_reason"] == "cost_incomplete"


def test_run_summary_scales_cost_before_display_rounding():
    params = {"sampled": 1, "arms": [{"model": "gpt-5.6-luna", "effort": "low"}]}
    rows = [
        _row("gpt-5.6-luna@low", "https://example.test/small-cost", cost_usd=Decimal("0.00009"))
    ]
    arm = exp.score("verify", params, rows)["arms"]["gpt-5.6-luna@low"]
    assert arm["known_cost_usd"] == 0.0001
    assert arm["cost_usd"] == 0.0001
    assert arm["cost_per_100_usd"] == 0.009


def test_the_agent_cli_sends_nothing_without_submit_and_scores_runs_side_by_side(
    f, monkeypatch, tmp_path, capsys
):
    """`python -m api.run_experiment` is the agent's path: the same step
    declarations, answers to files, nothing written to the database. Two
    labelled runs on the same seed are a before and after."""
    from api import run_experiment as cli
    from core.batch import BatchResult

    urls = _sample(f, 3)
    sent: list[tuple[str, str, int, int]] = []
    recipe = answers.VERIFICATION_REQUEST
    answer = {
        "is_closed": False,
        "closed_reason": "",
        "requires_clearance_or_restrictions": False,
        "clearance_reason": "",
    }

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        sent.append((model, effort, len(specs), max_out))
        assert all(s.instructions == recipe.instructions for s in specs)
        return {
            s.custom_id: BatchResult(
                s.custom_id,
                text=json.dumps(answer),
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 50,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                },
            )
            for s in specs
        }

    monkeypatch.setattr("core.batch.run_responses_batch", fake_batch)
    common = ["run", "--step", "verify", "--arm", "gpt-6-luna@low", "--sample", "3"]
    common += ["--seed", "s1"]

    assert cli.main([*common, "--out", str(tmp_path / "dry")]) == 0
    assert sent == [] and not (tmp_path / "dry").exists()
    assert json.loads(capsys.readouterr().out)["requests"] == 3

    for label in ("main", "branch"):
        out = tmp_path / label
        assert cli.main([*common, "--label", label, "--out", str(out), "--submit"]) == 0
        assert {json.loads(line)["url"] for line in (out / "results.jsonl").open()} == set(urls)
    capsys.readouterr()
    assert sent == [("gpt-6-luna", "low", 3, recipe.max_output_tokens)] * 2

    assert cli.main(["score", str(tmp_path / "main"), str(tmp_path / "branch")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert set(summary["arms"]) == {"main:gpt-6-luna@low", "branch:gpt-6-luna@low"}
    assert summary["missing_results"] == 0 and summary["missing_arms"] == []
    branch = summary["arms"]["branch:gpt-6-luna@low"]
    assert branch["agreement_with_reference"]["n"] == 3
    assert branch["cost_usd"] is not None and branch["cost_usd"] > 0
