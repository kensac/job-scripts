from __future__ import annotations

import re
from typing import Any

# Structured per-user criteria applied to source-derived visibility and to
# filter-run candidacy (so AI never spends on jobs a user excludes outright).
# Param-guarded so the clauses collapse to TRUE when a criterion is unset.
# Location exclusions match two ways, and either hides the posting. By word,
# on word boundaries rather than bare substrings ("UK" excludes "London, UK"
# and not "Tukwila, WA"), which is the rule from before places existed and
# still covers a string not yet classified. And by place: the posting's
# location and the criterion are both rows of `locations`, and a criterion
# excludes a location when every level the criterion names matches - a
# country-only criterion ("UK") takes every location in that country
# ("London"), a city criterion takes that city, and a bare "Remote" criterion
# takes remote postings. An unclassified location matches no place, so a
# never-seen string never hides a posting by mistake.
SQL = """
        AND (%(crit_date)s::date IS NULL OR j.date_posted >= %(crit_date)s::date)
        AND (NOT %(crit_has_excl)s OR NOT EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              LEFT JOIN locations l ON l.text = btrim(loc)
              WHERE EXISTS (SELECT 1 FROM unnest(%(crit_excl)s::text[]) ex
                            WHERE lower(loc) ~ ('\\m' || ex || '\\M'))
                 OR EXISTS (SELECT 1 FROM locations x
                            WHERE x.text = ANY(%(crit_excl_raw)s::text[])
                              AND ((x.country IS NULL AND x.remote AND l.remote)
                                   OR (x.country IS NOT NULL AND x.country = l.country
                                       AND (x.region IS NULL OR x.region = l.region)
                                       AND (x.city IS NULL OR x.city = l.city))))))
"""


def params(settings_row: dict[str, Any] | None) -> dict[str, Any]:
    crit = (settings_row or {}).get("criteria") or {}
    raw = [s.strip() for s in crit.get("excluded_locations", []) if s.strip()]
    excl = [re.escape(s.lower()) for s in raw]
    return {
        "crit_date": crit.get("date_posted_after"),
        "crit_excl": excl,
        "crit_excl_raw": raw,
        "crit_has_excl": bool(excl),
    }
