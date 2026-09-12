"""Admission and shadow reports for versioned job profiles."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, events
from api.auth import AuthedUser
from api.queue import enqueue
from api.routers.admin.shared import require_admin
from core.job_profile import CLASSIFIER_VERSION, JOB_PROFILE_MODEL
from tasks.job_profiles import BACKFILL_SELECTION_VERSION

router = APIRouter()


class JobProfileAdmission(BaseModel):
    task_id: int


class JobProfileTaskStatus(BaseModel):
    id: int
    status: str
    progress: dict | None
    error: str | None
    created_at: datetime.datetime
    finished_at: datetime.datetime | None


class JobProfilePopulation(BaseModel):
    value: str
    count: int


class JobProfileReport(BaseModel):
    classifier_version: str
    model: str
    classifications: int
    distinct_jobs: int
    superseded_observations: int
    output_tokens_p95: float | None
    output_tokens_p99: float | None
    output_tokens_max: int | None
    finish_reasons: list[JobProfilePopulation]
    role_families: list[JobProfilePopulation]
    career_stages: list[JobProfilePopulation]
    latest_task: JobProfileTaskStatus | None


@dataclass(frozen=True)
class _TaskId:
    id: int


@dataclass(frozen=True)
class _Counts:
    classifications: int
    distinct_jobs: int
    superseded_observations: int


@dataclass(frozen=True)
class _OutputStats:
    output_tokens_p95: float | None
    output_tokens_p99: float | None
    output_tokens_max: int | None


BACKFILL_MAX_AGE_DAYS = 7
BACKFILL_DEDUPE_KEY = (
    f"job-profile-backfill:{BACKFILL_SELECTION_VERSION}:"
    f"{BACKFILL_MAX_AGE_DAYS}:{CLASSIFIER_VERSION}:{JOB_PROFILE_MODEL}"
)


def admit_recent_backfill() -> int:
    payload = {
        "selection_version": BACKFILL_SELECTION_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "max_age_days": BACKFILL_MAX_AGE_DAYS,
        "all_eligible": True,
    }
    task_id = enqueue("classify_job_profiles", payload, dedupe_key=BACKFILL_DEDUPE_KEY)
    if task_id is not None:
        return task_id
    existing = db.query_one("SELECT id FROM tasks WHERE dedupe_key=%s", (BACKFILL_DEDUPE_KEY,))
    if existing is None:
        raise RuntimeError("job profile backfill admission lost its dedupe row")
    return existing["id"]


def _latest() -> JobProfileTaskStatus | None:
    return db.query_one_as(
        JobProfileTaskStatus,
        "SELECT id, status, progress, error, created_at, finished_at FROM tasks "
        "WHERE kind = 'classify_job_profiles' ORDER BY id DESC LIMIT 1",
    )


@router.post("/job-profiles/run")
def run_job_profiles(user: AuthedUser = Depends(require_admin)) -> JobProfileAdmission:
    with db.transaction():
        existing = db.query_one_as(
            _TaskId,
            "SELECT id FROM tasks WHERE kind = 'classify_job_profiles' "
            "AND status IN ('pending', 'running', 'awaiting_batch', 'waiting') "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
        )
        if existing:
            return JobProfileAdmission(task_id=existing.id)
        inserted = db.query_one_as(
            _TaskId,
            "INSERT INTO tasks (kind, payload) VALUES ('classify_job_profiles', '{}') RETURNING id",
        )
        assert inserted is not None
        task_id = inserted.id
    events.publish_task(task_id)
    return JobProfileAdmission(task_id=task_id)


@router.post("/job-profiles/backfill-recent")
def run_recent_job_profile_backfill(
    user: AuthedUser = Depends(require_admin),
) -> JobProfileAdmission:
    return JobProfileAdmission(task_id=admit_recent_backfill())


@router.get("/job-profiles/report")
def job_profile_report(user: AuthedUser = Depends(require_admin)) -> JobProfileReport:
    counts = db.query_one_as(
        _Counts,
        """
        SELECT count(*) AS classifications, count(DISTINCT url) AS distinct_jobs,
          count(*) FILTER (WHERE NOT EXISTS (
            SELECT 1 FROM ai_queries q WHERE q.id = p.content_row_id
              AND q.id = (SELECT q2.id FROM ai_queries q2 WHERE q2.url = p.url
                AND q2.check_type = 'content' AND q2.status = 'passed'
                AND q2.input_content IS NOT NULL ORDER BY q2.id DESC LIMIT 1)
          )) AS superseded_observations
        FROM job_profiles p WHERE classifier_version = %s AND model = %s
        """,
        (CLASSIFIER_VERSION, JOB_PROFILE_MODEL),
    )
    assert counts is not None
    output_stats = db.query_one_as(
        _OutputStats,
        """
        SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY (r.response->'usage'->>'output_tokens')::int) AS output_tokens_p95,
          percentile_cont(0.99) WITHIN GROUP (ORDER BY (r.response->'usage'->>'output_tokens')::int) AS output_tokens_p99,
          max((r.response->'usage'->>'output_tokens')::int) AS output_tokens_max
        FROM batch_result_receipts r JOIN tasks t ON t.id = r.task_id
        WHERE t.kind = 'classify_job_profiles' AND r.response->'usage'->>'output_tokens' IS NOT NULL
        """,
    )
    assert output_stats is not None

    def population(column: str) -> list[JobProfilePopulation]:
        statement = {
            "primary_role_family": "SELECT primary_role_family AS value, count(*) AS count FROM job_profiles WHERE classifier_version = %s AND model = %s GROUP BY primary_role_family ORDER BY count DESC, value",
            "career_stage": "SELECT career_stage AS value, count(*) AS count FROM job_profiles WHERE classifier_version = %s AND model = %s GROUP BY career_stage ORDER BY count DESC, value",
        }[column]
        return db.query_as(JobProfilePopulation, statement, (CLASSIFIER_VERSION, JOB_PROFILE_MODEL))

    return JobProfileReport(
        classifier_version=CLASSIFIER_VERSION,
        model=JOB_PROFILE_MODEL,
        classifications=counts.classifications,
        distinct_jobs=counts.distinct_jobs,
        superseded_observations=counts.superseded_observations,
        output_tokens_p95=output_stats.output_tokens_p95,
        output_tokens_p99=output_stats.output_tokens_p99,
        output_tokens_max=output_stats.output_tokens_max,
        finish_reasons=db.query_as(
            JobProfilePopulation,
            "SELECT COALESCE(response->>'finish_reason', 'unknown') AS value, count(*) AS count "
            "FROM batch_result_receipts r JOIN tasks t ON t.id = r.task_id "
            "WHERE t.kind = 'classify_job_profiles' GROUP BY value ORDER BY count DESC, value",
        ),
        role_families=population("primary_role_family"),
        career_stages=population("career_stage"),
        latest_task=_latest(),
    )
