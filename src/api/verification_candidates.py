"""Shared, cheap admission boundary for new-posting verification."""

from api import db, user_settings
from api.board import criteria
from core import screening
from core.store import AI_ELIGIBLE_JOB, ON_A_BOARD
from core.volume_gate import VolumeGate

# A board's title gate, and the screen title_screens names for the target's
# prompt: a posting either skips is never judged for that target.
_TITLE_SQL = (
    "AND NOT "
    + screening.skips_sql("target.title_gate->>'recipe'")
    + " AND NOT "
    + screening.skips_sql("%(title_screens)s::jsonb ->> target.prompt_hash")
)

TARGETS = f"""
verification_targets AS (
    SELECT us.source, COALESCE(settings.criteria, '{{}}'::jsonb) AS criteria,
           NULL::jsonb AS title_gate, filter.prompt_hash
    FROM users u
    JOIN user_source_set us ON us.user_id = u.id
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
# title the occupation_words_v1 screen skips. core.volume_gate.VolumeGate says
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

# The postings every target reads (no sources row, or someone tracks it). With
# the gate off, the broad legacy predicate, and no target reads anything else.
# Formatted over a `jobs` row aliased j; reachable()'s pool carries it.
EVERY_TARGET = f"""
    (NOT %(verification_reachability_gate_enabled)s AND {AI_ELIGIBLE_JOB.format(job="j")})
    OR (%(verification_reachability_gate_enabled)s AND (
        NOT EXISTS (SELECT 1 FROM sources source WHERE source.name = j.source)
        OR {ON_A_BOARD.format(job="j")}))
"""

# A target's criteria split by what a check costs. The places probe the
# locations table for each of a posting's locations; the rest compare columns.
_PLACES = ("excluded_locations", "included_locations")
_COLUMNS = tuple(name for name in criteria.CONDITIONS if name not in _PLACES)


def reachable(pool: str) -> str:
    """SELECT id, date_posted of the postings in `pool` the verification gate
    admits. `pool` is a relation of (id, every_target), every_target being
    EVERY_TARGET over the posting. Needs TARGETS in the WITH.

    The same predicate as one EXISTS per posting over the targets, staged for
    volume. As that EXISTS it ran as a subplan per posting, re-expanding the
    targets each time, and the verify sweep's candidate query ran past 14
    minutes on production (2026-10-10), holding locks that a deploy's DROP
    VIEW queued behind. Each stage is a fenced CTE, because the planner
    otherwise runs the costliest checks first on the most rows. Criteria
    depend on the posting and the criteria alone, and the three published
    boards share one, so they are checked per distinct (source, criteria)
    before the targets are expanded for the title checks. On that day:
    - 435,000 postings in the pool; every_target was read in the scan that
      built it;
    - the checks that compare columns kept 272,000 (posting, criteria) rows;
    - the place checks, about 0.05 ms each, ran once per distinct (criteria,
      locations): 67,000 times rather than 272,000. 119,000 rows passed;
    - the title checks ran on the 282,000 (posting, target) pairs those
      expand to, and admitted 40,000 postings.
    """
    return f"""
    SELECT j.id, j.date_posted FROM jobs j
    WHERE j.id IN (SELECT id FROM {pool} WHERE every_target)
    UNION
    SELECT targeted.id, targeted.date_posted FROM (
        WITH reader AS MATERIALIZED (
            SELECT DISTINCT source, criteria FROM verification_targets
        ),
        dated AS MATERIALIZED (
            SELECT j.id, j.locations, reader.source, reader.criteria
            FROM jobs j JOIN reader ON reader.source = j.source
            WHERE j.id IN (SELECT id FROM {pool})
                AND %(verification_reachability_gate_enabled)s
                {criteria.json_sql("reader.criteria", _COLUMNS)}
        ),
        placed AS MATERIALIZED (
            SELECT j.criteria, j.locations
            FROM (SELECT DISTINCT criteria, locations FROM dated) j
            WHERE TRUE {criteria.json_sql("j.criteria", _PLACES)}
        ),
        matched AS MATERIALIZED (
            SELECT id, source, criteria FROM dated
            WHERE (criteria, locations) IN (SELECT criteria, locations FROM placed)
        )
        SELECT j.id, j.date_posted
        FROM matched
        JOIN jobs j ON j.id = matched.id
        JOIN verification_targets target
          ON target.source = matched.source AND target.criteria = matched.criteria
        WHERE TRUE
            {_TITLE_SQL}
            {_VOLUME_SKIP}
    ) targeted
    """


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
        "title_screens": db.jsonb(db.get_config("title_screens")),
        **screening.PARAMS,
    }
