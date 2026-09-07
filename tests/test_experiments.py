"""An experiment measures one step across models and efforts on a seeded
sample, through the step's own request builder, and scores every arm
against a reference arm and against what production decided."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import db
from api.tasks import experiments as exp


def _verdict(url: str, rejected: bool) -> str:
    return json.dumps({"should_filter": rejected, "reason": "because"})


def _sample(f, n: int) -> list[str]:
    urls = []
    for i in range(n):
        _, url = f.make_ready_job(url=f"https://x.test/exp{i}", content="a real posting body " * 40)
        urls.append(url)
    return urls


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

    async def fake_submit(specs, model, effort, max_out, on_event=None):
        submitted.append((model, effort, len(specs)))
        assert all("only backend" in s.instructions for s in specs)
        assert all(s.input.startswith("Company: ") for s in specs)
        return [f"batch-{model}-{effort}"]

    monkeypatch.setattr("core.batch.submit_responses_batches", fake_submit)
    db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (task_id,))
    with pytest.raises(exp.AwaitingBatch):
        await exp.handle_run_experiment(task_id, {"experiment_id": eid})
    assert sorted(submitted) == [("gpt-5-nano", "medium", 4), ("gpt-5.6-luna", "high", 4)]
    parked = db.query_one("SELECT status, payload FROM tasks WHERE id = %s", (task_id,))
    assert parked["status"] == "awaiting_batch"
    assert sorted(parked["payload"]["batch_ids"]) == [
        "batch-gpt-5-nano-medium",
        "batch-gpt-5.6-luna-high",
    ]

    # The provider answers: luna agrees with production; nano rejects everything.
    async def fake_collect(task_id, hook):
        out = {}
        for u in urls:
            out[f"gpt-5-nano@medium|{u}"] = SimpleNamespace(
                text=_verdict(u, True),
                error=None,
                batch_id="b1",
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "output_tokens_details": {"reasoning_tokens": 400},
                },
            )
            out[f"gpt-5.6-luna@high|{u}"] = SimpleNamespace(
                text=_verdict(u, urls.index(u) >= 2),
                error=None,
                batch_id="b2",
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "output_tokens_details": {"reasoning_tokens": 50},
                },
            )
        db.execute("UPDATE tasks SET payload = payload - 'batch_ids' WHERE id = %s", (task_id,))
        return out

    monkeypatch.setattr(exp, "collect_pending", fake_collect)
    db.execute("UPDATE tasks SET status = 'running' WHERE id = %s", (task_id,))
    await exp.handle_run_experiment(task_id, {"experiment_id": eid})

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
    assert usage["n"] == 8 and usage["t"] == 8 * 1000 + 4 * 500 + 4 * 100
    # The listing carries what a form needs: the steps, each chat model
    # with the efforts it accepts, and every filter the filter step can name.
    listing = client.get("/v1/admin/experiments", headers=admin_headers).json()
    assert listing["steps"] == ["comp", "filter", "requirements", "verify"]
    efforts = {m["model"]: m["efforts"] for m in listing["models"]}
    assert "minimal" in efforts["gpt-5-nano"] and "none" in efforts["gpt-5.6-luna"]
    assert "text-embedding-3-small" not in efforts
    assert [(x["id"], x["name"]) for x in listing["filters"]] == [(fid, "strict")]
    assert listing["filters"][0]["user_email"] == "admin@example.com"
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
