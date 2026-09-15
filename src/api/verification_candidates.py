"""Shared, cheap admission boundary for new-posting verification."""

from api import db
from api.board import criteria
from core.managed_board_title_gate import sql_for_json
from core.store import AI_ELIGIBLE_JOB

_TITLE_SQL, PARAMS = sql_for_json("target.title_gate")

TARGETS = """
verification_targets AS (
    SELECT us.source, COALESCE(settings.criteria, '{}'::jsonb) AS criteria,
           NULL::jsonb AS title_gate
    FROM users u
    JOIN user_sources us ON us.user_id = u.id
    JOIN user_filters filter ON filter.user_id = u.id AND filter.enabled
    LEFT JOIN user_settings settings ON settings.user_id = u.id
    WHERE settings.api_key_enc IS NOT NULL
       OR u.groups && ARRAY(SELECT group_name FROM group_budgets)::text[]
    UNION
    SELECT source.source, board.criteria, board.title_gate
    FROM managed_boards board
    JOIN managed_board_sources source ON source.managed_board_id = board.id
    WHERE board.published
)
"""

REACHABLE = f"""
(
    (NOT %(verification_reachability_gate_enabled)s AND {AI_ELIGIBLE_JOB.format(job="j")})
    OR (%(verification_reachability_gate_enabled)s AND (
    NOT EXISTS (SELECT 1 FROM sources source WHERE source.name = j.source)
    OR EXISTS (SELECT 1 FROM user_jobs tracked WHERE tracked.job_id = j.id)
    OR EXISTS (
        SELECT 1 FROM verification_targets target
        WHERE target.source = j.source
        {criteria.json_sql("target.criteria")}
        {_TITLE_SQL}
    )))
)
"""

# The broad legacy predicate remains explicit in the disabled branch. This is
# useful in measurements and guards accidental widening if its semantics grow.
LEGACY_REACHABLE = AI_ELIGIBLE_JOB.format(job="j")


def params() -> dict[str, object]:
    return {
        "verification_reachability_gate_enabled": bool(
            db.get_config("verification_reachability_gate_enabled")
        ),
        **PARAMS,
    }
