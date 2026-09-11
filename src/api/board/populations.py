"""Distinct stored populations that the legacy user_jobs name collapsed."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from api import db
from api.board.person_state import UNTOUCHED


@dataclass(frozen=True)
class PopulationRow:
    generated_at: datetime.datetime
    source: str | None
    legacy_compatibility: int
    person_state: int
    working_set: int
    materialized_visibility: int
    person_and_working_set: int
    person_and_visibility: int
    working_set_and_visibility: int
    all_three: int
    person_shaped_unmarked: int
    legacy_unknown: int
    visibility_computed_at_min: datetime.datetime | None
    visibility_computed_at_max: datetime.datetime | None


def read(*, user_ids: list[int], sources: list[str]) -> list[PopulationRow]:
    """Read the selected pair sets and their overlaps from one DB snapshot."""
    user_scope = "AND pairs.user_id = ANY(%(user_ids)s)" if user_ids else ""
    source_scope = "AND j.source = ANY(%(sources)s)" if sources else ""
    return db.query_as(
        PopulationRow,
        f"""
        WITH signals AS MATERIALIZED (
            SELECT uj.user_id, uj.job_id, j.source,
                   true AS legacy, uj.person_touched_at IS NOT NULL AS person,
                   false AS working, false AS visible,
                   uj.person_touched_at IS NULL AND NOT ({UNTOUCHED}) AS shaped_unmarked,
                   uj.person_touched_at IS NULL AND ({UNTOUCHED}) AS unknown_candidate,
                   NULL::timestamptz AS visibility_computed_at
            FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
            WHERE true {user_scope.replace("pairs.", "uj.")} {source_scope}
            UNION ALL
            SELECT ws.user_id, ws.job_id, j.source,
                   false, false, true, false, false, false, NULL::timestamptz
            FROM user_job_working_set ws JOIN jobs j ON j.id = ws.job_id
            WHERE true {user_scope.replace("pairs.", "ws.")} {source_scope}
            UNION ALL
            SELECT bv.user_id, bv.job_id, j.source,
                   false, false, false, true, false, false, bv.computed_at
            FROM board_visible bv JOIN jobs j ON j.id = bv.job_id
            WHERE true {user_scope.replace("pairs.", "bv.")} {source_scope}
        ), pairs AS MATERIALIZED (
            SELECT user_id, job_id, source,
                   bool_or(legacy) AS legacy, bool_or(person) AS person,
                   bool_or(working) AS working, bool_or(visible) AS visible,
                   bool_or(shaped_unmarked) AS shaped_unmarked,
                   bool_or(unknown_candidate) AS unknown_candidate,
                   min(visibility_computed_at) AS visibility_computed_at
            FROM signals GROUP BY user_id, job_id, source
        ), grouped AS (
            SELECT source,
                   count(*) FILTER (WHERE legacy) AS legacy_compatibility,
                   count(*) FILTER (WHERE person) AS person_state,
                   count(*) FILTER (WHERE working) AS working_set,
                   count(*) FILTER (WHERE visible) AS materialized_visibility,
                   count(*) FILTER (WHERE person AND working) AS person_and_working_set,
                   count(*) FILTER (WHERE person AND visible) AS person_and_visibility,
                   count(*) FILTER (WHERE working AND visible) AS working_set_and_visibility,
                   count(*) FILTER (WHERE person AND working AND visible) AS all_three,
                   count(*) FILTER (WHERE shaped_unmarked) AS person_shaped_unmarked,
                   count(*) FILTER (WHERE unknown_candidate AND NOT working) AS legacy_unknown,
                   min(visibility_computed_at) FILTER (WHERE visible)
                       AS visibility_computed_at_min,
                   max(visibility_computed_at) FILTER (WHERE visible)
                       AS visibility_computed_at_max,
                   GROUPING(source) AS is_global
            FROM pairs GROUP BY GROUPING SETS ((), (source))
        )
        SELECT now() AS generated_at, source, legacy_compatibility, person_state, working_set,
               materialized_visibility, person_and_working_set, person_and_visibility,
               working_set_and_visibility, all_three, person_shaped_unmarked, legacy_unknown,
               visibility_computed_at_min, visibility_computed_at_max
        FROM grouped ORDER BY is_global DESC, source
        """,
        {"user_ids": user_ids, "sources": sources},
    )
