"""Selection for the versioned job-profile shadow derivation."""

from __future__ import annotations

import datetime
from typing import Any

from api import db
from core.job_profile import CLASSIFIER_VERSION, JOB_PROFILE_MODEL
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL, VERIFIED_OPEN

_ELIGIBLE = f"""
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id AS content_row_id, input_content, created_at AS content_created_at")}
    WHERE {AI_ELIGIBLE_JOB.format(job="j")}
      AND {VERIFIED_OPEN.format(url="j.url")}
      AND q.input_content IS NOT NULL AND q.input_content <> ''
      AND NOT EXISTS (
        SELECT 1 FROM job_profiles p
        WHERE p.content_row_id = q.content_row_id
          AND p.classifier_version = %(version)s AND p.model = %(model)s)
"""

PARAMS = {"version": CLASSIFIER_VERSION, "model": JOB_PROFILE_MODEL}


def candidates(
    cap: int | None,
    *,
    max_age_days: int | None = None,
    exclude_active_tasks: bool = False,
) -> list[dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = dict(PARAMS)
    if max_age_days is not None:
        if max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        params["created_since"] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            days=max_age_days
        )
        clauses.append("q.content_created_at >= %(created_since)s")
    if exclude_active_tasks:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM batch_requests br JOIN tasks t ON t.id=br.task_id "
            "WHERE t.kind='classify_job_profiles' "
            "AND t.status IN ('pending','running','waiting','awaiting_batch') "
            "AND br.custom_id=q.content_row_id::text)"
        )
    suffix = "".join(f" AND {clause}" for clause in clauses) + " ORDER BY j.id"
    if cap is not None:
        params["cap"] = cap
        suffix += " LIMIT %(cap)s"
    return db.query(
        "SELECT j.url, j.title, q.content_row_id, q.input_content " + _ELIGIBLE + suffix,
        params,
    )


def has_work() -> bool:
    return db.query_one("SELECT 1 " + _ELIGIBLE + " LIMIT 1", PARAMS) is not None
