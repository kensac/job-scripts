"""Selection for the versioned job-profile shadow derivation."""

from __future__ import annotations

from typing import Any

from api import db
from core.job_profile import CLASSIFIER_VERSION, JOB_PROFILE_MODEL
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL, VERIFIED_OPEN

_ELIGIBLE = f"""
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id AS content_row_id, input_content")}
    WHERE {AI_ELIGIBLE_JOB.format(job="j")}
      AND {VERIFIED_OPEN.format(url="j.url")}
      AND q.input_content IS NOT NULL AND q.input_content <> ''
      AND NOT EXISTS (
        SELECT 1 FROM job_profiles p
        WHERE p.content_row_id = q.content_row_id
          AND p.classifier_version = %(version)s AND p.model = %(model)s)
"""

PARAMS = {"version": CLASSIFIER_VERSION, "model": JOB_PROFILE_MODEL}


def candidates(cap: int) -> list[dict[str, Any]]:
    return db.query(
        "SELECT j.url, j.title, q.content_row_id, q.input_content "
        + _ELIGIBLE
        + " ORDER BY j.id LIMIT %(cap)s",
        {**PARAMS, "cap": cap},
    )


def has_work() -> bool:
    return db.query_one("SELECT 1 " + _ELIGIBLE + " LIMIT 1", PARAMS) is not None
