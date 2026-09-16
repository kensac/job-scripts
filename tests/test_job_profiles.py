from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import db
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


def _collection_enabled(enabled):
    db.execute(
        "INSERT INTO app_config (key,value) VALUES ('job_profile_collection_enabled',%s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (db.jsonb(enabled),),
    )


def test_disabled_profile_collection_blocks_manual_admission(client, admin_headers):
    _collection_enabled(False)
    response = client.post("/v1/admin/job-profiles/run", headers=admin_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROFILE_COLLECTION_PAUSED"
    assert (
        db.query_one("SELECT count(*) AS n FROM tasks WHERE kind='classify_job_profiles'")["n"] == 0
    )
    _collection_enabled(True)
    assert client.post("/v1/admin/job-profiles/run", headers=admin_headers).status_code == 200


def test_disabled_profile_collection_skips_scheduled_selection(f, monkeypatch):
    from api import worker

    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    f.make_ready_job(source=source)
    _collection_enabled(False)
    monkeypatch.setattr(worker, "INGEST_INTERVAL_MINUTES", 60)
    worker.schedule_ingest_cycle()
    assert (
        db.query_one("SELECT count(*) AS n FROM tasks WHERE kind='classify_job_profiles'")["n"] == 0
    )
    _collection_enabled(True)
    worker.schedule_ingest_cycle()
    assert (
        db.query_one("SELECT count(*) AS n FROM tasks WHERE kind='classify_job_profiles'")["n"] == 1
    )


@pytest.mark.asyncio
async def test_disabled_profile_collection_stops_already_queued_unpaid_task(f, monkeypatch):
    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    f.make_ready_job(source=source)
    task_id = f.make_task("classify_job_profiles", {}, status="running")
    _collection_enabled(False)

    async def no_submit(*args, **kwargs):
        pytest.fail("paused profile collection must not submit unpaid work")

    monkeypatch.setattr(job_profiles, "run_batched", no_submit)
    await job_profiles.handle_classify_job_profiles(task_id, {})
    assert (
        db.query_one("SELECT progress FROM tasks WHERE id=%s", (task_id,))["progress"]["label"]
        == "profile collection paused"
    )


@pytest.mark.asyncio
async def test_paused_collection_still_saves_paid_receipts_once(f, monkeypatch):
    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    _job_id, url = f.make_ready_job(source=source)
    row = job_profiles.job_profile_derivation.candidates(1)[0]
    task_id = f.make_task("classify_job_profiles", {}, status="running")
    spec = job_profiles.structured_response_spec(
        str(row["content_row_id"]),
        job_profiles.JOB_PROFILE_INSTRUCTIONS,
        job_profiles.build_job_profile_input(row["title"], row["input_content"]),
        JobProfileAnswer,
        context={
            "url": url,
            "content_row_id": row["content_row_id"],
            "content_hash": job_profiles._content_hash(row["input_content"]),
            "classifier_version": CLASSIFIER_VERSION,
        },
    )
    make_batch_result(task_id, spec, text=_answer(), model=JOB_PROFILE_MODEL)
    _collection_enabled(False)

    def no_selection(*args, **kwargs):
        pytest.fail("paid collection must not select new work")

    monkeypatch.setattr(job_profiles.job_profile_derivation, "candidates", no_selection)
    await job_profiles.handle_classify_job_profiles(task_id, {})
    assert db.query_one("SELECT url FROM job_profiles")["url"] == url
    assert db.query_one("SELECT outcome FROM batch_result_receipts")["outcome"] == "written"
    await job_profiles.handle_classify_job_profiles(task_id, {})
    assert db.query_one("SELECT count(*) AS n FROM job_profiles")["n"] == 1


@pytest.mark.asyncio
async def test_pause_during_selection_prevents_submission(f, monkeypatch):
    task_id = f.make_task("classify_job_profiles", {}, status="running")
    source = f.make_source()
    user_id = f.make_user()
    f.subscribe(user_id, source)
    f.make_ready_job(source=source)
    original = job_profiles.job_profile_derivation.candidates

    def select_then_pause(cap):
        rows = original(cap)
        _collection_enabled(False)
        return rows

    async def no_submit(*args, **kwargs):
        pytest.fail("a pause during preparation must prevent submission")

    monkeypatch.setattr(job_profiles.job_profile_derivation, "candidates", select_then_pause)
    monkeypatch.setattr(job_profiles, "run_batched", no_submit)
    await job_profiles.handle_classify_job_profiles(task_id, {})
