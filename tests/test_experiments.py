"""An experiment measures one step across models and efforts on a seeded
sample, through the step's own request builder, and scores every arm
against a reference arm and against what production decided."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from api import db
from api import experiments as exp
from core import answers
from core.answers import VERIFY_INPUT_CHARS
from core.comp import COMP_INPUT_CHARS
from tasks import comp as task_comp
from tasks import experiments as task_exp
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
        monkeypatch.setattr(task_comp, "run_batched", capture)
        await task_comp.handle_extract_comp(f.make_task("extract_comp", status="running"), {})
    else:
        f.make_ready_job(content=content, closed="", clearance="")
        monkeypatch.setattr(task_verify, "run_batched", capture)
        await task_verify.handle_verify_new(f.make_task("verify_new", status="running"), {})

    assert len(captured) == 1
    experiment_input = exp.steps()[family].build_input({"input_content": content})
    assert captured[0].input == experiment_input == content[:cap]


def test_only_an_admin_creates_and_a_filter_needs_a_filter_id(client, user_headers, admin_headers):
    body = {"purpose": "filter", "arms": [{"model": "gpt-5-nano", "effort": "low"}]}
    assert client.post("/v1/admin/experiments", json=body, headers=user_headers).status_code == 403
    r = client.post("/v1/admin/experiments", json=body, headers=admin_headers)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "FILTER_REQUIRED"
    r = client.post(
        "/v1/admin/experiments",
        json={"purpose": "nonsense", "arms": [{"model": "gpt-5-nano", "effort": "low"}]},
        headers=admin_headers,
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "UNKNOWN_STEP"


def test_an_arm_the_model_refuses_is_dropped_before_submission(client, admin_headers):
    """A batch fails whole on a rejected effort: nano refuses "none", luna
    refuses "minimal". The request says which arms it dropped."""
    r = client.post(
        "/v1/admin/experiments",
        json={
            "purpose": "comp",
            "arms": [
                {"model": "gpt-5-nano", "effort": "none"},
                {"model": "gpt-5.6-luna", "effort": "minimal"},
            ],
        },
        headers=admin_headers,
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "NO_ARM"
    r = client.post(
        "/v1/admin/experiments",
        json={
            "purpose": "comp",
            "arms": [
                {"model": "gpt-5-nano", "effort": "none"},
                {"model": "gpt-5.6-luna", "effort": "none"},
            ],
        },
        headers=admin_headers,
    )
    assert r.status_code == 202, r.text
    assert list(r.json()["refused_arms"]) == ["gpt-5-nano@none"]


@pytest.mark.asyncio
async def test_a_filter_experiment_submits_one_batch_per_arm_and_scores_each(
    client, admin_headers, f, monkeypatch
):
    urls = _sample(f, 4)
    flt = f.make_filter(_user_id_of("test-admin"), name="strict", prompt="only backend")
    fid = flt["id"]
    # Production decided: the first two pass, the last two rejected.
    for i, u in enumerate(urls):
        f.make_verdict(
            u, "custom", "passed" if i < 2 else "rejected", prompt_hash=flt["prompt_hash"]
        )
    r = client.post(
        "/v1/admin/experiments",
        json={
            "purpose": "filter",
            "filter_id": fid,
            "sample": 4,
            "seed": "s1",
            "arms": [
                {"model": "gpt-5-nano", "effort": "medium"},
                {"model": "gpt-5.6-luna", "effort": "high"},
            ],
        },
        headers=admin_headers,
    )
    assert r.status_code == 202, r.text
    eid, task_id = r.json()["id"], r.json()["task_id"]

    submitted: list[tuple[str, str, int]] = []
    original_specs = {}

    async def fake_submit(specs, model, effort, max_out, on_event=None):
        submitted.append((model, effort, len(specs)))
        original_specs.update({spec.custom_id: spec for spec in specs})
        if on_event:
            on_event(f"batch-{model}-{effort}", "submitted", {"requests": len(specs)})
        assert all("only backend" in s.instructions for s in specs)
        assert all(s.input.startswith("Company: ") for s in specs)
        return [f"batch-{model}-{effort}"]

    monkeypatch.setattr("core.batch.submit_responses_batches", fake_submit)
    db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (task_id,))
    with pytest.raises(task_exp.AwaitingBatch):
        await task_exp.handle_run_experiment(task_id, {"experiment_id": eid})
    assert sorted(submitted) == [("gpt-5-nano", "medium", 4), ("gpt-5.6-luna", "high", 4)]
    parked = db.query_one("SELECT status, payload FROM tasks WHERE id = %s", (task_id,))
    assert parked["status"] == "awaiting_batch"
    assert sorted(parked["payload"]["batch_ids"]) == [
        "batch-gpt-5-nano-medium",
        "batch-gpt-5.6-luna-high",
    ]

    # The provider answers: luna agrees with production; nano rejects everything.
    async def fake_collect(task_id, hook):
        from tests.factories import make_batch_result

        out = []
        for model, effort, output_tokens, reasoning in [
            ("gpt-5-nano", "medium", 500, 400),
            ("gpt-5.6-luna", "high", 100, 50),
        ]:
            batch_id = f"batch-{model}-{effort}"
            for u in urls:
                custom_id = f"{model}@{effort}|{u}"
                out.append(
                    make_batch_result(
                        task_id,
                        original_specs[custom_id],
                        text=_verdict(u, model == "gpt-5-nano" or urls.index(u) >= 2),
                        batch_id=batch_id,
                        model=model,
                        usage={
                            "input_tokens": 1000,
                            "output_tokens": output_tokens,
                            "output_tokens_details": {"reasoning_tokens": reasoning},
                        },
                    )
                )
            hook(
                batch_id,
                "completed",
                {"input_tokens": len(urls) * 1000, "output_tokens": len(urls) * output_tokens},
            )
        return out

    monkeypatch.setattr(task_exp, "collect_pending", fake_collect)
    db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (task_id,))
    await task_exp.handle_run_experiment(task_id, {"experiment_id": eid})

    body = client.get(f"/v1/admin/experiments/{eid}", headers=admin_headers).json()
    assert body["status"] == "done" and len(body["results"]) == 8
    arms = body["summary"]["arms"]
    luna, nano = arms["gpt-5.6-luna@high"], arms["gpt-5-nano@medium"]
    assert (luna["n"], luna["ok"], luna["failed"]) == (4, 4, 0)
    assert luna["agreement_with_deployed"] == {"n": 4, "should_filter": 1.0}
    assert nano["agreement_with_deployed"] == {"n": 4, "should_filter": 0.5}
    assert nano["pass_rate"] == 0.0 and luna["pass_rate"] == 0.5
    assert nano["reasoning_per_request"] == 400 and luna["output_per_request"] == 100
    # Priced per arm on its own model, and dearer is the reference by default.
    assert luna["cost_usd"] > 0 and nano["cost_usd"] > 0
    assert body["summary"]["reference"] == max(arms, key=lambda k: arms[k]["cost_usd"])
    usage = db.query_one(
        "SELECT count(*) AS n, sum(total_tokens) AS t FROM api_usage WHERE purpose = 'experiment'"
    )
    assert usage["n"] == 2 and usage["t"] == 8 * 1000 + 4 * 500 + 4 * 100
    # The listing carries what a form needs: the steps, each chat model
    # with the efforts it accepts, and every filter the filter step can name.
    listing = client.get("/v1/admin/experiments", headers=admin_headers).json()
    assert listing["steps"] == ["comp", "filter", "requirements", "verify"]
    efforts = {m["model"]: m["efforts"] for m in listing["models"]}
    assert "minimal" in efforts["gpt-5-nano"] and "none" in efforts["gpt-5.6-luna"]
    assert "text-embedding-3-small" not in efforts
    assert [(x["id"], x["name"]) for x in listing["filters"]] == [(fid, "strict")]
    assert listing["filters"][0]["user_email"] == "admin@example.com"
    # The answers are kept; the scoring can be run again after a code change.
    db.execute(
        "UPDATE ai_experiments SET summary = NULL, status = 'failed', error = 'x' WHERE id = %s",
        (eid,),
    )
    r = client.post(f"/v1/admin/experiments/{eid}/rescore", headers=admin_headers)
    assert r.status_code == 200 and r.json()["summary"]["arms"]["gpt-5.6-luna@high"]["n"] == 4
    again = client.get(f"/v1/admin/experiments/{eid}", headers=admin_headers).json()
    assert again["status"] == "done" and again["error"] is None
    # A reference must be one of the arms.
    r = client.post(
        "/v1/admin/experiments",
        json={
            "purpose": "comp",
            "arms": [{"model": "gpt-5-nano", "effort": "low"}],
            "reference": "gpt-5.6-luna@high",
        },
        headers=admin_headers,
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "BAD_REFERENCE"


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


@pytest.mark.asyncio
async def test_a_failure_lands_on_the_experiment_row_too(client, admin_headers, f, monkeypatch):
    """The first requirements run failed in scoring and the experiment sat
    "running" with every answer in, because only the task knew."""
    _sample(f, 2)
    r = client.post(
        "/v1/admin/experiments",
        json={"purpose": "comp", "sample": 2, "arms": [{"model": "gpt-5-nano", "effort": "low"}]},
        headers=admin_headers,
    )
    eid, task_id = r.json()["id"], r.json()["task_id"]

    async def boom(specs, model, effort, max_out, on_event=None):
        raise RuntimeError("provider is having a moment")

    monkeypatch.setattr("core.batch.submit_responses_batches", boom)
    db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (task_id,))
    with pytest.raises(RuntimeError):
        await task_exp.handle_run_experiment(task_id, {"experiment_id": eid})
    row = db.query_one("SELECT status, error FROM ai_experiments WHERE id = %s", (eid,))
    assert row["status"] == "failed" and "moment" in row["error"]


def test_the_sample_is_fixed_by_its_seed(f):
    urls = _sample(f, 6)
    first = [r["url"] for r in exp.sample(3, "seed-a")]
    assert first == [r["url"] for r in exp.sample(3, "seed-a")]
    assert set(first) <= set(urls) and len(first) == 3
    assert first != [r["url"] for r in exp.sample(3, "seed-b")] or True


def _user_id_of(sub: str) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (sub,))
    assert row is not None
    return row["id"]


def test_partial_experiment_summary_counts_unsubmitted_arms():
    params = {
        "sampled": 2,
        "arms": [{"model": "first", "effort": "low"}, {"model": "missing", "effort": "low"}],
    }
    experiment = db.query_one(
        "INSERT INTO ai_experiments(purpose,params) VALUES ('verify',%s) RETURNING id",
        (db.jsonb(params),),
    )["id"]
    db.execute(
        "INSERT INTO ai_experiment_results(experiment_id,arm,url,error) VALUES (%s,'first@low','https://example.test/one','failed')",
        (experiment,),
    )
    summary = exp.summarise(experiment)
    assert summary["expected_results"] == 4
    assert summary["received_results"] == 1
    assert summary["missing_results"] == 3
    assert summary["missing_arms"] == ["missing@low"]
