"""Compensation is bought only after a board or a person needs the posting."""

from api.board import criteria
from api.board.person_state import UNTOUCHED
from core.store import AI_ELIGIBLE_JOB

DEMANDED = f"""
(
    j.uploaded_by IS NOT NULL
    OR EXISTS (
        SELECT 1 FROM user_jobs uj WHERE uj.job_id = j.id
        AND (uj.person_touched_at IS NOT NULL OR NOT ({UNTOUCHED})))
    OR EXISTS (
        SELECT 1 FROM user_filters f
        JOIN user_sources s ON s.user_id = f.user_id AND s.source = j.source
        LEFT JOIN user_settings settings ON settings.user_id = f.user_id
        WHERE f.enabled
        {criteria.json_sql("settings.criteria")}
        AND (SELECT verdict.status FROM ai_queries verdict
             WHERE verdict.url = j.url AND verdict.check_type = 'custom'
               AND verdict.prompt_hash = f.prompt_hash
               AND verdict.status IN ('passed', 'rejected')
             ORDER BY verdict.id DESC LIMIT 1) = 'passed')
    OR EXISTS (
        SELECT 1 FROM managed_board_jobs membership
        JOIN managed_boards board ON board.id = membership.managed_board_id
        WHERE membership.job_id = j.id AND board.published)
)
"""

ELIGIBLE = f"""
CASE WHEN %(compensation_demand_gate_enabled)s THEN {DEMANDED}
     ELSE {AI_ELIGIBLE_JOB.format(job="j")} END
"""
