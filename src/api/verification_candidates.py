"""Shared, cheap admission boundary for new-posting verification."""

from api import db, user_settings
from api.board import criteria
from api.review_gate import load_policy
from core import screening
from core.review_gate import VolumeGate
from core.store import AI_ELIGIBLE_JOB

# A board's enforced title gate, and the screen filter_review_gate applies to
# the target's prompt: a posting either skips would never be judged for it.
_TITLE_SQL = (
    "AND (COALESCE(target.title_gate->>'mode', 'shadow') <> 'enforce' OR NOT "
    + screening.skips_sql("target.title_gate->>'recipe'")
    + ") AND NOT "
    + screening.skips_sql("%(review_title_recipes)s::jsonb ->> target.prompt_hash")
)

TARGETS = f"""
verification_targets AS (
    SELECT us.source, COALESCE(settings.criteria, '{{}}'::jsonb) AS criteria,
           NULL::jsonb AS title_gate, filter.prompt_hash
    FROM users u
    JOIN user_sources us ON us.user_id = u.id
    JOIN user_filters filter ON filter.user_id = u.id AND filter.enabled
    {user_settings.join("settings", "u.id")}
    WHERE {user_settings.has_own_key_sql("u.id")}
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

# A target in the volume gate's scopes does not read a posting from a source
# that boards and filters do not keep, or whose source and title have been
# judged often with no keep (bar a fixed sample of urls for both), or whose
# title the occupation_words_v1 screen skips. core.review_gate.VolumeGate says
# why.
_VOLUME_SKIP = (
    """
        AND NOT (target.prompt_hash = ANY(%(volume_gate_scopes)s::text[]) AND (
            ((j.source = ANY(%(volume_gate_sources)s::text[])
              OR j.source || E'\\x1f' || """
    + TITLE_KEY
    + """ = ANY(%(volume_gate_title_keys)s::text[]))
             AND abs(hashtext(j.url)) %% 100 >= %(volume_gate_audit_percent)s)
            OR (%(volume_gate_titles)s AND """
    + screening.skips_sql("'occupation_words_v1'")
    + """)))
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
            f"SELECT j.source, {TITLE_KEY} AS title FROM verdicts q "
            "JOIN jobs j ON j.url = q.url "
            "WHERE q.check_type = 'custom' "
            "AND q.created_at >= now() - make_interval(days => %s) "
            "GROUP BY 1, 2 HAVING count(*) >= %s AND NOT bool_or(q.status = 'passed')",
            (gate.window_days, gate.title_min_judged),
        )
    )


def unproductive_sources(gate: VolumeGate) -> list[str]:
    """Sources judged source_min_judged times in the window at or below the keep rate.

    Postings are counted once whichever board or filter judged them, and a
    posting any of them kept counts as kept.
    """
    if not gate.scopes or not gate.source_min_judged:
        return []
    return [
        row["source"]
        for row in db.query(
            "SELECT j.source FROM verdicts q JOIN jobs j ON j.url = q.url "
            "WHERE q.check_type = 'custom' "
            "AND q.created_at >= now() - make_interval(days => %s) "
            "GROUP BY j.source HAVING count(DISTINCT q.url) >= %s "
            "AND count(DISTINCT q.url) FILTER (WHERE q.status = 'passed') "
            "<= %s * count(DISTINCT q.url) ORDER BY j.source",
            (gate.window_days, gate.source_min_judged, gate.source_max_keep_rate),
        )
    ]


def params() -> dict[str, object]:
    gate = VolumeGate.model_validate(db.get_config("verification_volume_gate"))
    return {
        "verification_reachability_gate_enabled": bool(
            db.get_config("verification_reachability_gate_enabled")
        ),
        "volume_gate_scopes": gate.scopes,
        "volume_gate_sources": unproductive_sources(gate),
        "volume_gate_title_keys": unproductive_titles(gate),
        "volume_gate_audit_percent": gate.audit_percent,
        "volume_gate_titles": gate.occupation_titles,
        "review_title_recipes": db.jsonb(load_policy().title_recipes()),
        **screening.PARAMS,
    }
