from contextlib import nullcontext

import pytest

from api import ai, db, filter_routing
from api.config import CONFIG_KEYS
from core.filter_policy import ProfilePolicy, RouteProposal, RoutingPolicy, propose
from core.job_profile import JOB_PROFILE_MODEL, JobProfileAnswer
from core.profile_rules import ProfileRules
from tasks import filter_execution, job_profiles
from tests.factories import make_batch_result
from tests.test_title_screen import artifact


def profile(**overrides):
    return JobProfileAnswer(
        **{
            "primary_role_family": "engineering",
            "role_tracks": ["backend"],
            "career_stage": "entry",
            "employment_type": "full_time",
            "organization_sector": "software",
            "people_manager": False,
            "company_selectivity": "unknown",
            "role_selectivity": "unknown",
            **overrides,
        }
    )


def policy(**overrides):
    return RoutingPolicy(
        **{
            "profile_mode": "shadow",
            "ambiguity_mode": "shadow",
            "profiles": {
                "filter-hash": ProfilePolicy(
                    rules=ProfileRules(allowed_role_families=("engineering",)),
                )
            },
            **overrides,
        }
    )


def test_partial_rules_cannot_replace_prose_and_revisions_do_not_leak():
    answer = propose(policy(), "filter-hash", profile())
    assert answer.outcome == "abstain"
    assert answer.reason == "uncovered_filter_requirements"
    assert answer.would_review
    assert propose(policy(), "other-hash", profile()).outcome == "abstain"
    assert propose(policy(), "filter-hash", None).outcome == "abstain"


def test_route_controls_are_independent_and_live_enforcement_is_refused():
    other = profile(primary_role_family="sales")
    assert propose(policy(), "filter-hash", other).outcome == "reject"
    assert not propose(policy(), "filter-hash", other).would_review
    assert propose(policy(ambiguity_mode="off"), "filter-hash", other).would_review
    assert propose(policy(profile_mode="off"), "filter-hash", other).outcome == "abstain"
    complete = ProfilePolicy(
        rules=ProfileRules(allowed_role_families=("engineering",)),
        covers_entire_filter=True,
    )
    assert (
        propose(policy(profiles={"filter-hash": complete}), "filter-hash", profile()).outcome
        == "accept"
    )
    with pytest.raises(ValueError):
        CONFIG_KEYS["filter_routing_policy"].validate({"profile_mode": "enforce"})
    assert CONFIG_KEYS["filter_routing_policy"].validate(policy().model_dump(mode="json"))


def seed_profile(f):
    _, url = f.make_ready_job(content="original input")
    content = db.query_one(
        "SELECT id FROM ai_queries WHERE url = %s AND check_type = 'content'", (url,)
    )
    job_profiles._store(
        url,
        {
            "content_row_id": content["id"],
            "content_hash": job_profiles._content_hash("original input"),
        },
        profile(primary_role_family="sales"),
        JOB_PROFILE_MODEL,
    )
    job = db.query_one("SELECT url, title, company FROM jobs WHERE url = %s", (url,))
    return job


def test_profile_observation_uses_frozen_content_and_supported_version(f):
    job = seed_profile(f)
    url = job["url"]
    seen = filter_routing.observations(policy(), "filter-hash", [job], {url: "original input"})
    assert seen[url]["outcome"] == "reject"
    assert seen[url]["profile_id"] is not None
    changed = filter_routing.observations(policy(), "filter-hash", [job], {url: "new input"})
    assert changed[url]["outcome"] == "abstain"
    db.execute("UPDATE job_profiles SET classifier_version = 'superseded'")
    unsupported = filter_routing.observations(
        policy(), "filter-hash", [job], {url: "original input"}
    )
    assert unsupported[url]["outcome"] == "abstain"
    assert filter_routing.observations(None, "filter-hash", [job], {url: "original input"}) == {}


def test_bad_configuration_and_failed_observation_preserve_review(monkeypatch):
    monkeypatch.setattr(db, "get_config", lambda _key: {"profile_mode": "enforce"})
    assert filter_routing.load_policy() is None

    def unavailable(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "query", unavailable)
    assert filter_routing.observations(policy(), "filter-hash", [], {}) == {}


def test_profile_observer_restores_outer_transaction_timeout(f):
    job = seed_profile(f)
    with db.transaction():
        db.execute("SET LOCAL statement_timeout = '10s'")
        before = db.query_one("SELECT current_setting('statement_timeout') AS value")
        result = filter_routing.observations(
            policy(), "filter-hash", [job], {job["url"]: "original input"}
        )
        assert result[job["url"]]["outcome"] == "reject"
        assert db.query_one("SELECT current_setting('statement_timeout') AS value") == before


def test_title_observation_keeps_artifact_and_model_provenance():
    evidence = artifact()
    configured = RoutingPolicy(
        title_mode="shadow", ambiguity_mode="shadow", titles={"filter-revision": evidence}
    )
    jobs = [{"url": "https://posting.test", "title": "Senior Engineer"}]
    inputs = {"https://posting.test": "content"}
    proposal = filter_routing.observations(
        configured, "filter-revision", jobs, inputs, model="reference-model"
    )[jobs[0]["url"]]
    assert proposal["outcome"] == "reject"
    assert proposal["title_artifact"] == evidence.fingerprint
    unknown = filter_routing.observations(
        configured, "filter-revision", jobs, inputs, model="different-model"
    )[jobs[0]["url"]]
    assert unknown["outcome"] == "abstain"
    assert unknown["reason"] == "reference_model_mismatch"


def hooks():
    return filter_execution.ExecutionHooks(
        verdict_label="test-filter",
        key_source="owner",
        record_failure=lambda _: nullcontext(),
        record_usage=lambda *_: None,
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda *_: None,
        complete=lambda: None,
    )


@pytest.mark.asyncio
async def test_shadow_submits_every_job_and_compares_paid_results_once(f, monkeypatch):
    job = seed_profile(f)
    url = job["url"]
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('filter_routing_policy', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb(policy().model_dump(mode="json")),),
    )
    task_id = f.make_task("run_filter_batch_chunk", {}, status="running")
    asked = []
    receipts = []

    async def submit(task_id, specs, *_args):
        asked.extend(specs)
        receipts.extend(
            make_batch_result(task_id, spec, text='{"should_filter":false}', model="gpt-5.6-luna")
            for spec in specs
        )
        return receipts

    snapshot = filter_execution.FilterSnapshot("filter", "prompt", "filter", "filter-hash")
    await filter_execution.execute_batch(
        task_id,
        ai.AIConfig("openai", "key", "owner", "gpt-5.6-luna"),
        snapshot,
        [job],
        hooks(),
        contents={url: "original input"},
        unavailable=0,
        submit=submit,
    )
    assert len(asked) == 1
    assert asked[0].context["routing"]["outcome"] == "reject"
    assert asked[0].context["routing"]["would_review"] is False
    verdict = db.query_one("SELECT status FROM ai_queries WHERE check_type = 'custom'")
    assert verdict["status"] == "passed"
    payload = db.query_one("SELECT payload FROM tasks WHERE id = %s", (task_id,))["payload"]
    assert payload["routing_report"] == {"false_reject": 1}

    async def collect(*_args):
        return receipts

    def cannot_replan():
        raise AssertionError("paid work must never consult today's routing policy")

    monkeypatch.setattr(filter_execution, "has_batch_work", lambda _: True)
    monkeypatch.setattr(filter_routing, "load_policy", cannot_replan)
    await filter_execution.execute_batch(
        task_id,
        None,
        snapshot,
        [job],
        hooks(),
        contents={},
        unavailable=0,
        collect=collect,
    )
    payload = db.query_one("SELECT payload FROM tasks WHERE id = %s", (task_id,))["payload"]
    assert payload["routing_report"] == {"false_reject": 1}
    assert (
        db.query_one("SELECT count(*) AS n FROM ai_queries WHERE check_type = 'custom'")["n"] == 1
    )


def test_comparison_failure_cannot_abort_paid_result_transaction(f):
    task_id = f.make_task("run_filter_batch_chunk", {"routing_report": {"agreed": "broken"}})
    proposal = RouteProposal(outcome="reject", stage="title", reason="test", would_review=False)
    with db.transaction():
        filter_routing.record_comparison(task_id, proposal.model_dump(), True)
        db.execute("UPDATE tasks SET error = 'still writable' WHERE id = %s", (task_id,))
    assert (
        db.query_one("SELECT error FROM tasks WHERE id = %s", (task_id,))["error"]
        == "still writable"
    )
