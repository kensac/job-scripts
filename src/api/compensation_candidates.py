"""Compensation is bought only after a board or a person needs the posting."""

from api import user_settings
from api.board import criteria
from api.board.person_state import PERSON_STATE
from core import verdict_reads
from core.store import AI_ELIGIBLE_JOB

_LATEST_FILTER = verdict_reads.latest_per(
    "url, prompt_hash",
    "url, prompt_hash, status",
    "check_type = 'custom' "
    "AND prompt_hash = ANY(ARRAY(SELECT prompt_hash FROM user_filters WHERE enabled))",
)

TARGETS = f"""
latest_filter AS MATERIALIZED (
    {_LATEST_FILTER}
),
compensation_demand AS MATERIALIZED (
    SELECT id FROM jobs WHERE uploaded_by IS NOT NULL
    UNION
    SELECT uj.job_id FROM user_jobs uj
    WHERE {PERSON_STATE}
    UNION
    SELECT j.id FROM latest_filter verdict
    JOIN jobs j ON j.url = verdict.url
    JOIN user_filters f ON f.prompt_hash = verdict.prompt_hash AND f.enabled
    JOIN user_sources s ON s.user_id = f.user_id AND s.source = j.source
    {user_settings.join("settings", "f.user_id")}
    WHERE verdict.status = 'passed'
    {criteria.json_sql("settings.criteria")}
    UNION
    SELECT membership.job_id FROM managed_board_jobs membership
    JOIN managed_boards board ON board.id = membership.managed_board_id
    WHERE board.published
)
"""

DEMANDED = "j.id IN (SELECT id FROM compensation_demand)"


def selection(enabled: bool) -> tuple[str, str]:
    if enabled:
        return f"WITH {TARGETS}", DEMANDED
    return "", AI_ELIGIBLE_JOB.format(job="j")
