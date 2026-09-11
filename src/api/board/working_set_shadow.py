"""Read-only comparison of legacy board scope with its proposed replacement."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from api import db
from api.board.person_state import UNTOUCHED
from core.shapes import REVERIFY_DAYS, REVERIFY_PER_CYCLE
from core.store import SUBSCRIBED_SOURCE

SAMPLE_LIMIT = 20
DIGEST_UNKNOWN_REASON = (
    "user_job_working_set has no admission timestamp, so newly admitted "
    "digest candidates cannot be reconstructed"
)


@dataclass(frozen=True)
class Comparison:
    old_total: int
    proposed_total: int
    both: int
    old_only: int
    proposed_only: int
    legacy_unknown: int
    old_only_known: int
    sample_old_only_known: list[str]
    sample_proposed_only: list[str]
    sample_legacy_unknown: list[str]


def _empty_comparison() -> Comparison:
    return Comparison(0, 0, 0, 0, 0, 0, 0, [], [], [])


@dataclass(frozen=True)
class UserComparison:
    user_id: int
    pair_membership: Comparison | None
    stale_reverify_candidates: Comparison | None
    digest_old_candidates: int
    digest_cannot_tell: int


@dataclass(frozen=True)
class ShadowReport:
    generated_at: datetime.datetime
    reverify_days: int
    reverify_per_cycle: int
    global_comparisons: dict[str, Comparison]
    users: list[UserComparison]


@dataclass(frozen=True)
class _ReportRow:
    generated_at: datetime.datetime
    row_kind: str
    surface: str
    scope_user_id: int | None
    old_total: int | None
    proposed_total: int | None
    matched_count: int | None
    old_only: int | None
    proposed_only: int | None
    legacy_unknown: int | None
    old_only_known: int | None
    sample_old_only_known: list[str]
    sample_proposed_only: list[str]
    sample_legacy_unknown: list[str]
    digest_old_candidates: int | None
    digest_cannot_tell: int | None


def report() -> ShadowReport:
    """Read every compared population and bounded sample from one snapshot."""
    rows = db.query_as(
        _ReportRow,
        f"""
        WITH
        legacy_pairs AS MATERIALIZED (
            SELECT user_id, job_id FROM user_jobs
        ),
        proposed_pairs AS MATERIALIZED (
            SELECT user_id, job_id FROM user_job_working_set
            UNION
            SELECT user_id, job_id FROM user_jobs WHERE person_touched_at IS NOT NULL
        ),
        unknown_pairs AS MATERIALIZED (
            SELECT uj.user_id, uj.job_id
            FROM user_jobs uj LEFT JOIN proposed_pairs proposed USING (user_id, job_id)
            WHERE proposed.job_id IS NULL AND uj.person_touched_at IS NULL AND {UNTOUCHED}
        ),
        independently_eligible AS MATERIALIZED (
            SELECT DISTINCT j.id AS job_id FROM jobs j
            WHERE {SUBSCRIBED_SOURCE.format(source="j.source")}
               OR NOT EXISTS (SELECT 1 FROM sources s WHERE s.name = j.source)
        ),
        old_eligible AS MATERIALIZED (
            SELECT job_id FROM independently_eligible UNION SELECT job_id FROM legacy_pairs
        ),
        proposed_eligible AS MATERIALIZED (
            SELECT job_id FROM independently_eligible UNION SELECT job_id FROM proposed_pairs
        ),
        old_stale_pairs AS MATERIALIZED (
            SELECT DISTINCT uj.user_id, uj.job_id
            FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
            WHERE {UNTOUCHED}
              AND COALESCE((SELECT MAX(q.created_at) FROM ai_queries q
                            WHERE q.url = j.url AND q.check_type = 'closed'), '-infinity')
                  < now() - make_interval(days => %(days)s)
        ),
        proposed_stale_pairs AS MATERIALIZED (
            SELECT DISTINCT ws.user_id, ws.job_id
            FROM user_job_working_set ws JOIN jobs j ON j.id = ws.job_id
            WHERE COALESCE((SELECT MAX(q.created_at) FROM ai_queries q
                            WHERE q.url = j.url AND q.check_type = 'closed'), '-infinity')
                  < now() - make_interval(days => %(days)s)
        ),
        relisted AS MATERIALIZED (
            SELECT j.id AS job_id FROM jobs j
            WHERE j.active
              AND (SELECT MAX(e.at) FROM job_listing_events e
                   WHERE e.job_id = j.id AND e.listed) > COALESCE(
                    (SELECT MAX(q.created_at) FROM ai_queries q
                     WHERE q.url = j.url AND q.check_type = 'closed'), '-infinity')
        ),
        old_stale_jobs AS MATERIALIZED (
            SELECT job_id FROM old_stale_pairs
            UNION SELECT job_id FROM relisted JOIN old_eligible USING (job_id)
        ),
        proposed_stale_jobs AS MATERIALIZED (
            SELECT job_id FROM proposed_stale_pairs
            UNION SELECT job_id FROM relisted JOIN proposed_eligible USING (job_id)
        ),
        old_scheduled AS MATERIALIZED (
            SELECT id AS user_id FROM users u
            WHERE EXISTS (SELECT 1 FROM user_sources s WHERE s.user_id = u.id)
               OR EXISTS (SELECT 1 FROM legacy_pairs p WHERE p.user_id = u.id)
               OR EXISTS (SELECT 1 FROM jobs j WHERE j.uploaded_by = u.id)
        ),
        proposed_scheduled AS MATERIALIZED (
            SELECT id AS user_id FROM users u
            WHERE EXISTS (SELECT 1 FROM user_sources s WHERE s.user_id = u.id)
               OR EXISTS (SELECT 1 FROM proposed_pairs p WHERE p.user_id = u.id)
               OR EXISTS (SELECT 1 FROM jobs j WHERE j.uploaded_by = u.id)
        ),
        old_sets AS (
            SELECT 'pair_membership' AS surface, NULL::bigint AS scope_user_id,
                   user_id::text || ':' || job_id::text AS item,
                   EXISTS (SELECT 1 FROM unknown_pairs x
                           WHERE x.user_id = p.user_id AND x.job_id = p.job_id) AS unknown
            FROM legacy_pairs p
            UNION ALL
            SELECT 'pair_membership', user_id, job_id::text,
                   EXISTS (SELECT 1 FROM unknown_pairs x
                           WHERE x.user_id = p.user_id AND x.job_id = p.job_id)
            FROM legacy_pairs p
            UNION ALL
            SELECT 'ai_eligible_jobs', NULL, job_id::text,
                   NOT EXISTS (SELECT 1 FROM independently_eligible x WHERE x.job_id = e.job_id)
                   AND EXISTS (SELECT 1 FROM unknown_pairs x WHERE x.job_id = e.job_id)
            FROM old_eligible e
            UNION ALL
            SELECT 'stale_reverify_candidates', NULL, job_id::text,
                   EXISTS (SELECT 1 FROM old_stale_pairs s JOIN unknown_pairs x
                           USING (user_id, job_id) WHERE s.job_id = e.job_id)
            FROM old_stale_jobs e
            UNION ALL
            SELECT 'stale_reverify_candidates', user_id, job_id::text,
                   EXISTS (SELECT 1 FROM unknown_pairs x
                           WHERE x.user_id = s.user_id AND x.job_id = s.job_id)
            FROM old_stale_pairs s
            UNION ALL
            SELECT 'scheduled_users', NULL, user_id::text,
                   NOT EXISTS (SELECT 1 FROM user_sources s WHERE s.user_id = u.user_id)
                   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.uploaded_by = u.user_id)
                   AND EXISTS (SELECT 1 FROM unknown_pairs x WHERE x.user_id = u.user_id)
            FROM old_scheduled u
        ),
        proposed_sets AS (
            SELECT 'pair_membership' AS surface, NULL::bigint AS scope_user_id,
                   user_id::text || ':' || job_id::text AS item FROM proposed_pairs
            UNION ALL SELECT 'pair_membership', user_id, job_id::text FROM proposed_pairs
            UNION ALL SELECT 'ai_eligible_jobs', NULL, job_id::text FROM proposed_eligible
            UNION ALL SELECT 'stale_reverify_candidates', NULL, job_id::text FROM proposed_stale_jobs
            UNION ALL SELECT 'stale_reverify_candidates', user_id, job_id::text
                      FROM proposed_stale_pairs
            UNION ALL SELECT 'scheduled_users', NULL, user_id::text FROM proposed_scheduled
        ),
        aligned AS (
            SELECT COALESCE(old.surface, proposed.surface) AS surface,
                   COALESCE(old.scope_user_id, proposed.scope_user_id) AS scope_user_id,
                   COALESCE(old.item, proposed.item) AS item,
                   old.item IS NOT NULL AS in_old, proposed.item IS NOT NULL AS in_proposed,
                   COALESCE(old.unknown, false) AS unknown
            FROM old_sets old FULL JOIN proposed_sets proposed
              ON proposed.surface = old.surface
             AND proposed.scope_user_id IS NOT DISTINCT FROM old.scope_user_id
             AND proposed.item = old.item
        ),
        compared AS (
            SELECT surface, scope_user_id,
                   COUNT(*) FILTER (WHERE in_old) AS old_total,
                   COUNT(*) FILTER (WHERE in_proposed) AS proposed_total,
                   COUNT(*) FILTER (WHERE in_old AND in_proposed) AS matched_count,
                   COUNT(*) FILTER (WHERE in_old AND NOT in_proposed) AS old_only,
                   COUNT(*) FILTER (WHERE in_proposed AND NOT in_old) AS proposed_only,
                   COUNT(*) FILTER (WHERE in_old AND NOT in_proposed AND unknown)
                       AS legacy_unknown,
                   COUNT(*) FILTER (WHERE in_old AND NOT in_proposed AND NOT unknown)
                       AS old_only_known,
                   COALESCE((array_agg(item ORDER BY item) FILTER (
                       WHERE in_old AND NOT in_proposed AND NOT unknown
                   ))[1:%(sample)s], '{{}}') AS sample_old_only_known,
                   COALESCE((array_agg(item ORDER BY item)
                       FILTER (WHERE in_proposed AND NOT in_old))[1:%(sample)s], '{{}}')
                       AS sample_proposed_only,
                   COALESCE((array_agg(item ORDER BY item)
                       FILTER (WHERE in_old AND NOT in_proposed AND unknown))[1:%(sample)s], '{{}}')
                       AS sample_legacy_unknown
            FROM aligned GROUP BY surface, scope_user_id
        ),
        digest AS (
            SELECT u.id AS user_id,
                   (SELECT COUNT(*) FROM user_jobs uj
                    WHERE uj.user_id = u.id
                      AND uj.created_at > COALESCE(s.last_digest_at, now() - interval '1 day'))
                       AS old_candidates,
                   (SELECT COUNT(*) FROM user_job_working_set ws WHERE ws.user_id = u.id)
                       AS cannot_tell
            FROM users u LEFT JOIN user_settings s ON s.user_id = u.id
            WHERE EXISTS (SELECT 1 FROM legacy_pairs p WHERE p.user_id = u.id)
               OR EXISTS (SELECT 1 FROM proposed_pairs p WHERE p.user_id = u.id)
        )
        SELECT now() AS generated_at, 'comparison' AS row_kind, surface, scope_user_id,
               old_total, proposed_total, matched_count, old_only, proposed_only, legacy_unknown,
               old_only_known, sample_old_only_known, sample_proposed_only,
               sample_legacy_unknown,
               NULL::bigint AS digest_old_candidates, NULL::bigint AS digest_cannot_tell
        FROM compared
        UNION ALL
        SELECT now(), 'digest', 'digest_candidates', user_id,
               NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               '{{}}'::text[], '{{}}'::text[], '{{}}'::text[], old_candidates, cannot_tell
        FROM digest
        ORDER BY row_kind, surface, scope_user_id NULLS FIRST
        """,
        {"days": REVERIFY_DAYS, "sample": SAMPLE_LIMIT},
    )
    generated_at = rows[0].generated_at if rows else datetime.datetime.now(datetime.UTC)
    comparisons: dict[tuple[str, int | None], Comparison] = {}
    digests: dict[int, tuple[int, int]] = {}
    fields = (
        "old_total",
        "proposed_total",
        "old_only",
        "proposed_only",
        "legacy_unknown",
        "old_only_known",
        "sample_old_only_known",
        "sample_proposed_only",
        "sample_legacy_unknown",
    )
    for row in rows:
        if row.row_kind == "digest":
            assert row.scope_user_id is not None
            assert row.digest_old_candidates is not None
            assert row.digest_cannot_tell is not None
            digests[row.scope_user_id] = (
                row.digest_old_candidates,
                row.digest_cannot_tell,
            )
        else:
            values = {field: getattr(row, field) for field in fields}
            assert row.matched_count is not None
            assert all(value is not None for value in values.values())
            comparisons[(row.surface, row.scope_user_id)] = Comparison(
                both=row.matched_count, **values
            )
    user_ids = sorted({uid for _, uid in comparisons if uid is not None} | set(digests))
    global_comparisons = {
        surface: comparison for (surface, uid), comparison in comparisons.items() if uid is None
    }
    for surface in (
        "pair_membership",
        "ai_eligible_jobs",
        "stale_reverify_candidates",
        "scheduled_users",
    ):
        global_comparisons.setdefault(surface, _empty_comparison())
    return ShadowReport(
        generated_at=generated_at,
        reverify_days=REVERIFY_DAYS,
        reverify_per_cycle=REVERIFY_PER_CYCLE,
        global_comparisons=global_comparisons,
        users=[
            UserComparison(
                user_id=uid,
                pair_membership=comparisons.get(("pair_membership", uid)),
                stale_reverify_candidates=comparisons.get(("stale_reverify_candidates", uid)),
                digest_old_candidates=digests.get(uid, (0, 0))[0],
                digest_cannot_tell=digests.get(uid, (0, 0))[1],
            )
            for uid in user_ids
        ],
    )
