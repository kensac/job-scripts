"""Shared, cheap admission boundary for new-posting verification."""

from api import db
from api.board import criteria
from core.managed_board_title_gate import sql_for_json
from core.review_gate import OCCUPATION_SQL_PATTERN, TECHNICAL_SQL_PATTERN, VolumeGate
from core.store import AI_ELIGIBLE_JOB

_TITLE_SQL, PARAMS = sql_for_json("target.title_gate")

TARGETS = """
verification_targets AS (
    SELECT us.source, COALESCE(settings.criteria, '{}'::jsonb) AS criteria,
           NULL::jsonb AS title_gate, filter.prompt_hash
    FROM users u
    JOIN user_sources us ON us.user_id = u.id
    JOIN user_filters filter ON filter.user_id = u.id AND filter.enabled
    LEFT JOIN user_settings settings ON settings.user_id = u.id
    WHERE settings.api_key_enc IS NOT NULL
       OR u.groups && ARRAY(SELECT group_name FROM group_budgets)::text[]
    UNION
    SELECT source.source, board.criteria, board.title_gate, board.prompt_hash
    FROM managed_boards board
    JOIN managed_board_sources source ON source.managed_board_id = board.id
    WHERE board.published
)
"""

# How a title is compared: case and runs of whitespace do not make a new title.
TITLE_KEY = "lower(regexp_replace(j.title, '\\s+', ' ', 'g'))"

# A target in the volume gate's scopes does not read a posting whose source and
# title have been judged often with no keep (bar a fixed sample of its urls) or
# whose title names an occupation and no technical word.
# core.review_gate.VolumeGate says why, and why there is no whole-source rule.
_VOLUME_SKIP = (
    """
        AND NOT (target.prompt_hash = ANY(%(volume_gate_scopes)s::text[]) AND (
            (j.source || E'\\x1f' || """
    + TITLE_KEY
    + """ = ANY(%(volume_gate_title_keys)s::text[])
             AND abs(hashtext(j.url)) %% 100 >= %(volume_gate_audit_percent)s)
            OR (%(volume_gate_titles)s
                AND j.title !~* %(volume_gate_technical)s
                AND j.title ~* %(volume_gate_occupations)s)))
"""
)

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
        {_VOLUME_SKIP}
    )))
)
"""

# The broad legacy predicate remains explicit in the disabled branch. This is
# useful in measurements and guards accidental widening if its semantics grow.
LEGACY_REACHABLE = AI_ELIGIBLE_JOB.format(job="j")


def unproductive_titles(gate: VolumeGate) -> list[str]:
    """(source, title) keys judged title_min_judged times in the window, no keep.

    Every custom verdict counts, whichever board or filter gave it, so one keep
    anywhere returns the title to full reading.
    """
    if not gate.scopes or not gate.title_min_judged:
        return []
    return sorted(
        f"{row['source']}\x1f{row['title']}"
        for row in db.query(
            f"SELECT j.source, {TITLE_KEY} AS title FROM ai_queries q "
            "JOIN jobs j ON j.url = q.url "
            "WHERE q.check_type = 'custom' AND q.status IN ('passed', 'rejected') "
            "AND q.created_at >= now() - make_interval(days => %s) "
            "GROUP BY 1, 2 HAVING count(*) >= %s AND NOT bool_or(q.status = 'passed')",
            (gate.window_days, gate.title_min_judged),
        )
    )


def params() -> dict[str, object]:
    gate = VolumeGate.model_validate(db.get_config("verification_volume_gate"))
    return {
        "verification_reachability_gate_enabled": bool(
            db.get_config("verification_reachability_gate_enabled")
        ),
        "volume_gate_scopes": gate.scopes,
        "volume_gate_title_keys": unproductive_titles(gate),
        "volume_gate_audit_percent": gate.audit_percent,
        "volume_gate_titles": gate.occupation_titles,
        "volume_gate_technical": TECHNICAL_SQL_PATTERN,
        "volume_gate_occupations": OCCUPATION_SQL_PATTERN,
        **PARAMS,
    }
