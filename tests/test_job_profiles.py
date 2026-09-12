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
