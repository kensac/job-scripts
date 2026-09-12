from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import db
from api.ai import batch_results
from api.routers.admin import job_profiles as job_profiles_api
from core.job_profile import CLASSIFIER_VERSION, JOB_PROFILE_MODEL, JobProfileAnswer
from tasks import job_profiles
from tests.factories import make_batch_result


def _answer(**overrides) -> str:
    value = {
        "primary_role_family": "engineering",
        "role_tracks": ["backend", "platform"],
        "career_stage": "entry",
        "employment_type": "full_time",
        "organization_sector": "software",
        "people_manager": False,
        "company_selectivity": "selective",
        "role_selectivity": "highly_selective",
        **overrides,
    }
    return JobProfileAnswer(**value).model_dump_json()


def test_taxonomy_rejects_more_than_two_or_duplicate_tracks():
    with pytest.raises(ValueError):
        JobProfileAnswer.model_validate_json(
            _answer(role_tracks=["backend", "platform", "infrastructure"])
        )
    with pytest.raises(ValueError):
        JobProfileAnswer.model_validate_json(_answer(role_tracks=["backend", "backend"]))


@pytest.mark.asyncio
async def test_shadow_task_uses_exact_content_and_is_idempotent(f, monkeypatch):
    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    _job_id, url = f.make_ready_job(source=source, title="Platform Engineer")
    task_id = f.make_task("classify_job_profiles", {}, status="running")
    asked = []

    async def fake_run(task_id, shape, specs, **kwargs):
        assert kwargs == {"allow_configured_override": False}
        asked.extend(specs)
        return (
            [
                make_batch_result(
                    task_id,
                    spec,
                    text=_answer(),
                    model=JOB_PROFILE_MODEL,
                )
                for spec in specs
            ],
            SimpleNamespace(model=JOB_PROFILE_MODEL),
        )

    monkeypatch.setattr(job_profiles, "run_batched", fake_run)
    await job_profiles.handle_classify_job_profiles(task_id, {})
    assert len(asked) == 1
    content_id = int(asked[0].custom_id)
    row = db.query_one("SELECT * FROM job_profiles")
    assert row["url"] == url
    assert row["content_row_id"] == content_id
    assert row["classifier_version"] == CLASSIFIER_VERSION
    assert row["model"] == JOB_PROFILE_MODEL

    second = f.make_task("classify_job_profiles", {}, status="running")
    asked.clear()
    await job_profiles.handle_classify_job_profiles(second, {})
    assert asked == []
    assert db.query_one("SELECT count(*) AS n FROM job_profiles")["n"] == 1


@pytest.mark.asyncio
async def test_superseded_content_receipt_is_not_written(f, monkeypatch):
    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    _job_id, url = f.make_ready_job(source=source)
    task_id = f.make_task("classify_job_profiles", {}, status="running")

    async def fake_run(task_id, shape, specs, **kwargs):
        f.make_verdict(url, "content", "passed", content="replacement page " * 30)
        return (
            [make_batch_result(task_id, specs[0], text=_answer(), model=JOB_PROFILE_MODEL)],
            SimpleNamespace(model=JOB_PROFILE_MODEL),
        )

    monkeypatch.setattr(job_profiles, "run_batched", fake_run)
    await job_profiles.handle_classify_job_profiles(task_id, {})
    assert db.query_one("SELECT count(*) AS n FROM job_profiles")["n"] == 0
    assert db.query_one("SELECT outcome FROM batch_result_receipts")["outcome"] == "superseded"


def test_admin_trigger_is_idempotent_and_report_is_typed(client, admin_headers):
    first = client.post("/v1/admin/job-profiles/run", headers=admin_headers)
    second = client.post("/v1/admin/job-profiles/run", headers=admin_headers)
    assert first.status_code == 200 and second.json() == first.json()
    report = client.get("/v1/admin/job-profiles/report", headers=admin_headers)
    assert report.status_code == 200
    assert report.json()["classifier_version"] == CLASSIFIER_VERSION
    assert report.json()["model"] == JOB_PROFILE_MODEL
    assert report.json()["classifications"] == 0


def test_recent_backfill_admission_has_versioned_scope_and_is_idempotent(client, admin_headers):
    first = client.post("/v1/admin/job-profiles/backfill-recent", headers=admin_headers)
    second = client.post("/v1/admin/job-profiles/backfill-recent", headers=admin_headers)
    assert first.status_code == 200 and second.json() == first.json()
    task = db.query_one(
        "SELECT payload, dedupe_key FROM tasks WHERE id=%s", (first.json()["task_id"],)
    )
    assert task["payload"] == {
        "selection_version": job_profiles.BACKFILL_SELECTION_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "max_age_days": 7,
        "all_eligible": True,
    }
    assert task["dedupe_key"] == job_profiles_api.BACKFILL_DEDUPE_KEY


def test_backfill_admission_remains_idempotent_after_completion(client, admin_headers):
    first = client.post("/v1/admin/job-profiles/backfill-recent", headers=admin_headers).json()[
        "task_id"
    ]
    db.execute("UPDATE tasks SET status='done', finished_at=now() WHERE id=%s", (first,))
    second = client.post("/v1/admin/job-profiles/backfill-recent", headers=admin_headers).json()[
        "task_id"
    ]
    assert second == first
    assert db.query_one("SELECT count(*) AS n FROM tasks")["n"] == 1


@pytest.mark.asyncio
async def test_legacy_empty_payload_keeps_hourly_cap(monkeypatch, f):
    task_id = f.make_task("classify_job_profiles", {}, status="running")
    calls = []

    def candidates(cap, **kwargs):
        calls.append((cap, kwargs))
        return []

    monkeypatch.setattr(job_profiles.job_profile_derivation, "candidates", candidates)
    await job_profiles.handle_classify_job_profiles(task_id, {})
    assert calls == [(job_profiles.JOB_PROFILE_TASK.per_cycle, {})]


def test_recent_candidates_include_boundary_side_and_exclude_older(f):
    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    _recent_job, recent_url = f.make_ready_job(source=source)
    _old_job, old_url = f.make_ready_job(source=source)
    db.execute(
        "UPDATE ai_queries SET created_at=now() - interval '6 days 23 hours' "
        "WHERE url=%s AND check_type='content'",
        (recent_url,),
    )
    db.execute(
        "UPDATE ai_queries SET created_at=now() - interval '7 days 1 hour' "
        "WHERE url=%s AND check_type='content'",
        (old_url,),
    )
    rows = job_profiles.job_profile_derivation.candidates(None, max_age_days=7)
    assert {row["url"] for row in rows} == {recent_url}


def test_backfill_excludes_requests_frozen_by_any_active_profile_task(f):
    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    _first_job, first_url = f.make_ready_job(source=source)
    _second_job, second_url = f.make_ready_job(source=source)
    rows = job_profiles.job_profile_derivation.candidates(None, max_age_days=7)
    first = next(row for row in rows if row["url"] == first_url)
    active = f.make_task("classify_job_profiles", {}, status="awaiting_batch")
    spec = job_profiles.structured_response_spec(
        str(first["content_row_id"]),
        "frozen",
        "input",
        JobProfileAnswer,
    )
    batch_results.snapshot_specs(active, [spec])

    eligible = job_profiles.job_profile_derivation.candidates(
        None, max_age_days=7, exclude_active_tasks=True
    )
    assert {row["url"] for row in eligible} == {second_url}


@pytest.mark.asyncio
async def test_retry_uses_frozen_specs_without_reselecting(monkeypatch, f):
    task_id = f.make_task("classify_job_profiles", {}, status="running")
    spec = job_profiles.structured_response_spec(
        "123",
        "frozen instructions",
        "frozen input",
        JobProfileAnswer,
        context={"classifier_version": CLASSIFIER_VERSION},
    )
    batch_results.snapshot_specs(task_id, [spec])

    def must_not_select(*args, **kwargs):
        raise AssertionError("a snapshotted task reselected mutable candidates")

    async def capture(task_id, shape, specs, **kwargs):
        assert [item.custom_id for item in specs] == ["123"]
        assert specs[0].input == "frozen input"
        return [], SimpleNamespace(model=JOB_PROFILE_MODEL)

    monkeypatch.setattr(job_profiles.job_profile_derivation, "candidates", must_not_select)
    monkeypatch.setattr(job_profiles, "run_batched", capture)
    await job_profiles.handle_classify_job_profiles(task_id, {})


def test_scheduler_admits_backlog_once_and_skips_no_work(f, monkeypatch):
    from api import worker

    monkeypatch.setattr(worker, "INGEST_INTERVAL_MINUTES", 60)
    worker.schedule_ingest_cycle()
    assert (
        db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'classify_job_profiles'")["n"]
        == 0
    )

    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    f.make_ready_job(source=source)
    worker.schedule_ingest_cycle()
    worker.schedule_ingest_cycle()
    assert (
        db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'classify_job_profiles'")["n"]
        == 1
    )
