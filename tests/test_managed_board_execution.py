from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from api import db
from core import pricing
from core.filters import compute_filter_hash
from core.store import add_ai_result
from tasks import managed_boards as managed_task


def test_worker_registry_declares_managed_board_handler():
    from tasks import HANDLERS

    assert HANDLERS["run_managed_board"] is managed_task.handle_run_managed_board
    assert HANDLERS["run_managed_board_batch"] is managed_task.handle_run_managed_board_batch


def _admissible(monkeypatch) -> None:
    from api import ai, budget

    monkeypatch.setattr(ai, "server_key", lambda provider: "test-server-key")
    monkeypatch.setattr(budget, "owner_budget", lambda groups: (True, 1_000_000))
    monkeypatch.setattr(budget, "owner_allowed_models", lambda groups: ["gpt-5.6-luna"])


def _board_and_job(client, admin_headers, f):
    source = f.make_source("managed-source")
    job_id, url = f.make_ready_job(source=source)
    response = client.post("/v1/admin/managed-boards/bootstrap", headers=admin_headers)
    assert response.status_code == 200, response.text
    board = response.json()["boards"][0]
    return board, job_id, url


def test_run_admission_snapshots_board_candidates_and_refuses_overlap(
    client, admin_headers, f, monkeypatch
):
    _admissible(monkeypatch)
    board, job_id, _url = _board_and_job(client, admin_headers, f)

    response = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["candidate_count"] == 1 and queued["reserved_tokens"] > 0
    payload = db.query_one("SELECT payload FROM tasks WHERE id = %s", (queued["task_id"],))[
        "payload"
    ]
    assert db.query_one("SELECT kind FROM tasks WHERE id = %s", (queued["task_id"],))["kind"] == (
        "run_managed_board_batch"
    )
    assert payload["revision"] == board["revision"]
    assert payload["requested_model"] == "gpt-5.6-luna"
    assert payload["execution_version"] == 2
    assert payload["inference_transport"] == "batch"
    assert payload["reasoning_effort"] == "low"
    assert payload["sources"] == ["managed-source"]
    assert [job["id"] for job in payload["jobs"]] == [job_id]
    assert payload["jobs"][0]["content_query_id"] is not None
    assert "content" not in payload["jobs"][0]

    conflict = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "IN_PROGRESS",
        "message": "this board already has an active run",
        "task_id": queued["task_id"],
    }


def test_sponsor_budget_reserves_across_managed_boards(client, admin_headers, f, monkeypatch):
    _admissible(monkeypatch)
    boards = _board_and_job(client, admin_headers, f)[0]
    first = client.post(f"/v1/admin/managed-boards/{boards['id']}/run", headers=admin_headers)
    assert first.status_code == 200

    from api import budget

    monkeypatch.setattr(
        budget, "owner_budget", lambda groups: (True, first.json()["reserved_tokens"])
    )
    second_board = client.get("/v1/admin/managed-boards", headers=admin_headers).json()["boards"][2]
    refused = client.post(
        f"/v1/admin/managed-boards/{second_board['id']}/run", headers=admin_headers
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "BUDGET_EXCEEDED"
    assert db.query_one("SELECT count(*) AS n FROM tasks")["n"] == 1


@pytest.mark.asyncio
async def test_handler_attributes_usage_and_atomically_replaces_projection(f, monkeypatch):
    """A pre-cutover payload remains receivable through its original live path."""
    from api import ai

    monkeypatch.setattr(ai, "server_key", lambda provider: "test-server-key")
    sponsor = f.make_user(groups=["infra-admins"])
    source = f.make_source("managed-source")
    job_id, url = f.make_ready_job(source=source)
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model, on_ambiguous, fail_closed, criteria) "
        "VALUES ('managed', 'Managed', %s, 'prompt', %s, 'gpt-5.6-luna', 'filter', true, '{}') "
        "RETURNING id, revision",
        (sponsor, compute_filter_hash("prompt", "filter")),
    )
    assert board is not None
    payload = {
        "managed_board_id": board["id"],
        "sponsor_user_id": sponsor,
        "revision": board["revision"],
        "prompt": "prompt",
        "prompt_hash": compute_filter_hash("prompt", "filter"),
        "requested_model": "gpt-5.6-luna",
        "on_ambiguous": "filter",
        "fail_closed": True,
        "sources": [source],
        "criteria": {},
        "published": False,
        "reserved_tokens": 1000,
        "jobs": [
            {
                "id": job_id,
                "url": url,
                "company": "Acme",
                "title": "Engineer",
                "sort_at": "2026-09-01T00:00:00+00:00",
            }
        ],
    }
    task_id = f.make_task("run_managed_board", payload, status="running")

    async def fake_execute(task_id, cfg, snapshot, jobs, hooks):
        add_ai_result(
            url,
            "passed",
            check_type="custom",
            prompt_hash=snapshot.prompt_hash,
            model=cfg.model,
        )
        hooks.record_usage(
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}, cfg.model, False
        )
        hooks.complete()

    monkeypatch.setattr(managed_task, "execute_live", fake_execute)
    await managed_task.handle_run_managed_board(task_id, payload)

    assert db.query_one("SELECT managed_board_id, user_id, total_tokens FROM api_usage") == {
        "managed_board_id": board["id"],
        "user_id": None,
        "total_tokens": 12,
    }
    assert db.query_one(
        "SELECT job_id, projection_revision, resolved_model FROM managed_board_jobs"
    ) == {
        "job_id": job_id,
        "projection_revision": board["revision"],
        "resolved_model": "gpt-5.6-luna",
    }


@pytest.mark.asyncio
async def test_new_execution_contract_uses_batch_only_and_prices_batch(f, monkeypatch):
    from api import ai

    monkeypatch.setattr(ai, "server_key", lambda provider: "test-server-key")
    sponsor = f.make_user(groups=["infra-admins"])
    source = f.make_source("managed-batch-source")
    job_id, url = f.make_ready_job(source=source)
    prompt_hash = compute_filter_hash("prompt", "filter")
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model, on_ambiguous, fail_closed, criteria) "
        "VALUES ('managed-batch', 'Managed batch', %s, 'prompt', %s, "
        "'gpt-5.6-luna', 'filter', true, '{}') RETURNING id, revision",
        (sponsor, prompt_hash),
    )
    payload = {
        "managed_board_id": board["id"],
        "sponsor_user_id": sponsor,
        "revision": board["revision"],
        "prompt": "prompt",
        "prompt_hash": prompt_hash,
        "requested_model": "gpt-5.6-luna",
        "execution_mode": "managed_filter",
        "execution_version": 2,
        "inference_transport": "batch",
        "reasoning_effort": "medium",
        "on_ambiguous": "filter",
        "fail_closed": True,
        "sources": [source],
        "criteria": {},
        "published": False,
        "reserved_tokens": 1000,
        "jobs": [
            {
                "id": job_id,
                "url": url,
                "company": "Acme",
                "title": "Engineer",
                "sort_at": "2026-09-01T00:00:00+00:00",
            }
        ],
    }
    task_id = f.make_task("run_managed_board_batch", payload, status="running")

    async def fake_batch(task_id, cfg, snapshot, jobs, hooks, **kwargs):
        assert kwargs["purpose"] == "managed_board"
        # No cap: a managed board submits like tasks/filters.py does,
        # which passes none and takes execute_batch's 6000 default.
        # Capping at 120 truncated the JSON, and 14.8% of one board's
        # verdicts were recorded as failed then dropped by fail_closed.
        assert "max_output_tokens" not in kwargs
        assert kwargs["complete_without_submission"] is True
        assert cfg.model == "gpt-5.6-luna"
        hooks.record_usage(
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            cfg.model,
            True,
        )
        add_ai_result(url, "passed", check_type="custom", prompt_hash=prompt_hash, model=cfg.model)
        db.execute(
            "UPDATE tasks SET payload = jsonb_set(payload, '{batch_ids}', '[\"still-running\"]') "
            "WHERE id = %s",
            (task_id,),
        )
        hooks.complete()
        assert db.query_one("SELECT count(*) AS n FROM managed_board_jobs")["n"] == 0
        db.execute(
            "UPDATE tasks SET payload = jsonb_set(payload, '{batch_ids}', '[]') WHERE id = %s",
            (task_id,),
        )
        hooks.complete()

    monkeypatch.setattr(managed_task, "execute_batch", fake_batch)
    monkeypatch.setattr(
        managed_task,
        "execute_live",
        lambda *args, **kwargs: pytest.fail("versioned managed runs must never execute live"),
    )
    await managed_task.handle_run_managed_board_batch(task_id, payload)

    usage = db.query_one("SELECT managed_board_id, batched, total_tokens, cost_usd FROM api_usage")
    assert usage["managed_board_id"] == board["id"]
    assert usage["batched"] is True
    assert usage["total_tokens"] == 12
    assert usage["cost_usd"] == Decimal("0.000002")
    assert pricing.estimate_cost_usd(
        "gpt-5.6-luna", 10, 2, batched=True
    ) < pricing.estimate_cost_usd("gpt-5.6-luna", 10, 2, batched=False)
    assert db.query_one("SELECT job_id FROM managed_board_jobs")["job_id"] == job_id


@pytest.mark.asyncio
async def test_versioned_managed_run_never_falls_back_for_an_unknown_contract(f):
    payload = {
        "execution_mode": "managed_filter",
        "execution_version": 2,
        "inference_transport": "live",
    }
    with pytest.raises(ValueError, match="unsupported execution contract"):
        await managed_task.handle_run_managed_board_batch(
            f.make_task("run_managed_board_batch", payload), payload
        )


@pytest.mark.asyncio
async def test_legacy_handler_refuses_versioned_managed_work(f):
    payload = {
        "execution_mode": "managed_filter",
        "execution_version": 2,
        "inference_transport": "batch",
    }
    with pytest.raises(ValueError, match="requires the batch task kind"):
        await managed_task.handle_run_managed_board(
            f.make_task("run_managed_board", payload), payload
        )


def test_admission_overlap_includes_legacy_task_kind(client, admin_headers, f, monkeypatch):
    _admissible(monkeypatch)
    board, _job_id, _url = _board_and_job(client, admin_headers, f)
    legacy = f.make_task(
        "run_managed_board",
        {"managed_board_id": board["id"]},
        status="awaiting_batch",
    )

    response = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["task_id"] == legacy


@pytest.mark.asyncio
async def test_sponsor_filter_reuse_projects_only_exact_machine_results_without_calls(
    client, admin_headers, f, monkeypatch
):
    from api import budget

    source = f.make_source("personal-source")
    other_source = f.make_source("person-only-source")
    sponsor = db.query_one("SELECT id FROM users WHERE sub = 'test-admin'")["id"]
    f.subscribe(sponsor, source)
    flt = f.make_filter(sponsor, prompt="backend only", enabled=True)
    included_id, included_url = f.make_ready_job(source=source)
    rejected_id, rejected_url = f.make_ready_job(source=source)
    wrong_model_id, wrong_model_url = f.make_ready_job(source=source)
    person_only_id, _person_only_url = f.make_ready_job(source=other_source, uploaded_by=sponsor)
    f.make_board_row(sponsor, person_only_id, status="Saved")
    add_ai_result(
        included_url,
        "passed",
        check_type="custom",
        prompt_hash=flt["prompt_hash"],
        model="gpt-5.6-luna",
    )
    add_ai_result(
        rejected_url,
        "rejected",
        check_type="custom",
        prompt_hash=flt["prompt_hash"],
        model="gpt-5.6-luna",
    )
    add_ai_result(
        wrong_model_url,
        "passed",
        check_type="custom",
        prompt_hash=flt["prompt_hash"],
        model="gpt-5.5",
    )
    monkeypatch.setattr(
        budget,
        "load_config",
        lambda user_id, ignore_budget=False: (None, SimpleNamespace(model="gpt-5.6-luna")),
    )
    bootstrap = client.post("/v1/admin/managed-boards/bootstrap", headers=admin_headers)
    assert bootstrap.status_code == 200, bootstrap.text
    board = bootstrap.json()["boards"][2]

    queued = client.post(f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers)
    assert queued.status_code == 200, queued.text
    assert queued.json()["reserved_tokens"] == 0
    payload = db.query_one("SELECT payload FROM tasks WHERE id = %s", (queued.json()["task_id"],))[
        "payload"
    ]
    assert payload["execution_mode"] == "sponsor_filter_reuse"
    assert [job["id"] for job in payload["jobs"]] == [included_id]
    assert rejected_id not in [job["id"] for job in payload["jobs"]]
    assert wrong_model_id not in [job["id"] for job in payload["jobs"]]
    assert person_only_id not in [job["id"] for job in payload["jobs"]]

    monkeypatch.setattr(
        managed_task,
        "execute_live",
        lambda *args, **kwargs: pytest.fail("reuse mode must not make model calls"),
    )
    await managed_task.handle_run_managed_board(queued.json()["task_id"], payload)
    assert db.query_one("SELECT job_id, resolved_model FROM managed_board_jobs") == {
        "job_id": included_id,
        "resolved_model": "gpt-5.6-luna",
    }
    assert db.query_one("SELECT count(*) AS n FROM api_usage")["n"] == 0
    cost = client.get(f"/v1/admin/managed-boards/{board['id']}/cost", headers=admin_headers)
    assert cost.json()["calls"] == 0 and cost.json()["total_tokens"] == 0


def test_projection_revision_cas_preserves_previous_projection(f):
    from api import managed_board_runs

    sponsor = f.make_user()
    source = f.make_source()
    old_job = f.make_job(source=source)
    new_job = f.make_job(source=source)
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, requested_model) "
        "VALUES ('cas', 'CAS', %s, 'p', %s, 'gpt-5.6-luna') RETURNING id",
        (sponsor, compute_filter_hash("p")),
    )
    assert board is not None
    db.execute(
        "INSERT INTO managed_board_jobs (managed_board_id, job_id, sort_at, projection_revision) "
        "VALUES (%s, %s, now(), 1)",
        (board["id"], old_job),
    )
    db.execute("UPDATE managed_boards SET revision = 2 WHERE id = %s", (board["id"],))
    with pytest.raises(RuntimeError, match="configuration changed"):
        managed_board_runs.replace_projection(
            {
                "managed_board_id": board["id"],
                "revision": 1,
                "prompt_hash": compute_filter_hash("p"),
                "requested_model": "gpt-5.6-luna",
                "fail_closed": False,
                "jobs": [{"id": new_job, "sort_at": "2026-09-01T00:00:00+00:00"}],
            }
        )
    assert db.query_one("SELECT job_id FROM managed_board_jobs")["job_id"] == old_job


def test_latest_and_cost_routes_are_board_scoped(client, admin_headers, f, monkeypatch):
    _admissible(monkeypatch)
    board, _job_id, _url = _board_and_job(client, admin_headers, f)
    queued = client.post(
        f"/v1/admin/managed-boards/{board['id']}/run", headers=admin_headers
    ).json()
    legacy_id = f.make_task(
        "run_managed_board",
        {
            "managed_board_id": board["id"],
            "revision": board["revision"],
            "requested_model": "gpt-5.6-luna",
            "reserved_tokens": 0,
        },
        status="done",
    )
    db.execute(
        "INSERT INTO api_usage (managed_board_id, key_source, purpose, model, total_tokens, cost_usd) "
        "VALUES (%s, 'owner', 'managed_board', 'gpt-5.6-luna', 25, %s)",
        (board["id"], Decimal("0.012345")),
    )
    latest = client.get(
        f"/v1/admin/managed-boards/{board['id']}/runs/latest", headers=admin_headers
    ).json()["run"]
    assert queued["task_id"] < legacy_id
    assert latest["id"] == legacy_id
    assert latest["snapshot_revision"] == board["revision"]
    cost = client.get(f"/v1/admin/managed-boards/{board['id']}/cost", headers=admin_headers).json()
    assert cost["calls"] == cost["week_calls"] == 1
    assert cost["total_tokens"] == cost["week_tokens"] == 25
    assert cost["cost_usd"] == cost["week_cost_usd"] == 0.012345
