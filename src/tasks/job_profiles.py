"""Shadow job-profile extraction from immutable content observations."""

from __future__ import annotations

import hashlib
from typing import Any

from api import db, job_profile_derivation
from core.batch import BatchResult, BatchSpec
from core.job_profile import (
    CLASSIFIER_VERSION,
    JOB_PROFILE_INPUT_CHARS,
    JOB_PROFILE_MODEL,
    JobProfileAnswer,
    job_profile_spec,
)
from core.shapes import JOB_PROFILE_TASK
from tasks.derive import Derivation, Row


def _content_hash(content: str) -> str:
    frozen = content[:JOB_PROFILE_INPUT_CHARS]
    return hashlib.sha256(frozen.encode()).hexdigest()


def _store(url: str, context: dict[str, Any], answer: JobProfileAnswer, model: str) -> None:
    db.execute(
        """
        INSERT INTO job_profiles
          (url, content_row_id, content_hash, classifier_version, model,
           primary_role_family, role_tracks, career_stage, employment_type,
           organization_sector, people_manager, company_selectivity, role_selectivity)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (content_row_id, classifier_version, model) DO NOTHING
        """,
        (
            url,
            context["content_row_id"],
            context["content_hash"],
            CLASSIFIER_VERSION,
            model,
            answer.primary_role_family,
            answer.role_tracks,
            answer.career_stage,
            answer.employment_type,
            answer.organization_sector,
            answer.people_manager,
            answer.company_selectivity,
            answer.role_selectivity,
        ),
    )


def _requests(rows: list[Row]) -> list[BatchSpec]:
    return [
        job_profile_spec(
            row["url"],
            row["content_row_id"],
            row["title"],
            row["input_content"],
            _content_hash(row["input_content"]),
        )
        for row in rows
    ]


def _store_result(result: BatchResult, context: dict[str, Any], answer: JobProfileAnswer) -> str:
    if context.get("classifier_version") != CLASSIFIER_VERSION:
        return "unknown_request"
    _store(context["url"], context, answer, result.model or JOB_PROFILE_MODEL)
    return "written"


# Off by config (job_profile_collection_enabled). Pausing keeps the stored
# profiles and lets paid batches collect their receipts. A new profile is
# keyed by (content_row_id, classifier_version, model), so the recipe version
# is part of the staleness rule: bumping it re-derives every profile.
PROFILES = Derivation(
    kind="classify_job_profiles",
    purpose=JOB_PROFILE_TASK.purpose,
    noun="job profile",
    table="job_profiles",
    per_cycle_key="job_profiles_per_cycle",
    select=lambda cap, payload: job_profile_derivation.candidates(cap),
    requests=_requests,
    store=_store_result,
    input_chars=JOB_PROFILE_INPUT_CHARS,
    recipe=CLASSIFIER_VERSION,
    shape=JOB_PROFILE_TASK,
    model=JOB_PROFILE_MODEL,
    answer=JobProfileAnswer,
    context_keys=("url", "content_row_id", "classifier_version"),
    switch="job_profile_collection_enabled",
    has_work=job_profile_derivation.has_work,
)
