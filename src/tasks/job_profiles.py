"""Shadow job-profile extraction from immutable content observations."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from api import db, job_profile_derivation
from api.ai import batch_results
from core.batch import structured_response_spec
from core.job_profile import (
    CLASSIFIER_VERSION,
    JOB_PROFILE_INPUT_CHARS,
    JOB_PROFILE_INSTRUCTIONS,
    JOB_PROFILE_MODEL,
    JobProfileAnswer,
    build_job_profile_input,
)
from core.shapes import JOB_PROFILE_TASK
from tasks import rescrape
from tasks.runtime import consume_result, has_batch_work, run_batched, set_progress

logger = logging.getLogger(__name__)

BACKFILL_SELECTION_VERSION = 1


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


async def handle_classify_job_profiles(task_id: int, payload: dict[str, Any]) -> None:
    resumed = has_batch_work(task_id)
    specs = batch_results.frozen_specs(task_id)
    if specs or resumed:
        rows = []
    elif payload.get("all_eligible") is True:
        if payload.get("selection_version") != BACKFILL_SELECTION_VERSION:
            raise ValueError("unsupported job profile selection version")
        max_age_days = payload.get("max_age_days")
        if not isinstance(max_age_days, int) or isinstance(max_age_days, bool):
            raise ValueError("max_age_days must be an integer")
        if payload.get("classifier_version") != CLASSIFIER_VERSION:
            raise ValueError("job profile classifier version changed after admission")
        rows = job_profile_derivation.candidates(
            None, max_age_days=max_age_days, exclude_active_tasks=True
        )
    else:
        rows = job_profile_derivation.candidates(JOB_PROFILE_TASK.per_cycle)
    new_specs = [
        structured_response_spec(
            str(row["content_row_id"]),
            JOB_PROFILE_INSTRUCTIONS,
            build_job_profile_input(row["title"], row["input_content"]),
            JobProfileAnswer,
            context={
                "url": row["url"],
                "content_row_id": row["content_row_id"],
                "content_hash": _content_hash(row["input_content"]),
                "classifier_version": CLASSIFIER_VERSION,
            },
        )
        for row in rows
    ]
    if not specs:
        specs = new_specs
    if not specs and not resumed:
        set_progress(task_id, 0, 0, "nothing to classify")
        return
    set_progress(task_id, 0, len(specs), "job profile batch")
    results, chosen = await run_batched(
        task_id, JOB_PROFILE_TASK, specs, allow_configured_override=False
    )
    if chosen.model is not None and chosen.model != JOB_PROFILE_MODEL:
        raise RuntimeError("job profile task resolved an unsupported model")
    for result in results:
        with consume_result(task_id, result) as receipt:
            if not receipt.pending:
                continue
            context = result.request.context if result.request else None
            if not context or context.get("classifier_version") != CLASSIFIER_VERSION:
                receipt.outcome = "unknown_request"
                continue
            if not rescrape.content_is_current(context["url"], context["content_row_id"]):
                receipt.outcome = "superseded"
                continue
            if result.error or not result.text:
                receipt.outcome = "provider_error"
                continue
            try:
                answer = JobProfileAnswer.model_validate_json(result.text)
            except ValueError:
                logger.warning("job profile parse failed for %s", context["url"])
                receipt.outcome = "malformed"
                continue
            _store(context["url"], context, answer, result.model or JOB_PROFILE_MODEL)
            receipt.outcome = "written"
    done, total = batch_results.progress_counts(task_id)
    set_progress(task_id, done, total, "job profiles classified")
