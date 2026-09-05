from __future__ import annotations

from typing import Any

# Structured per-user criteria applied to source-derived visibility and to
# filter-run candidacy (so AI never spends on jobs a user excludes outright).
# Param-guarded so the clauses collapse to TRUE when a criterion is unset.
# Location criteria match places. The posting's location and the criterion
# are both rows of `locations`, and a criterion matches a location when every
# level the criterion names matches: a country-only criterion ("UK") takes
# every location in that country ("London"), a city criterion that city, and
# a bare "Remote" criterion any remote posting. Excluded: a posting with any
# matching location is hidden. Included: a posting is visible only when one
# of its locations matches, so "United States" plus "Remote" is a board of US
# or remote postings and nothing else; a posting with no location at all
# stays, having nothing to judge. A string not yet classified matches
# nothing, for at most the one hourly cycle that classifies it. There is no
# word match beside this: the raw text cannot know that London is the UK.
# A string may name several places and so may a criterion ("United States
# and Canada"); the match is any of the string's against any of the
# criterion's, at every level the criterion's entry names.
_PLACE_MATCH = """
              (x.country IS NULL AND x.remote AND l.remote)
              OR EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(x.places) xp, jsonb_array_elements(l.places) lp
                  WHERE xp->>'country' = lp->>'country'
                    AND (xp->>'region' IS NULL OR xp->>'region' = lp->>'region')
                    AND (xp->>'city' IS NULL OR xp->>'city' = lp->>'city'))"""

SQL = f"""
        AND (%(crit_date)s::date IS NULL OR j.date_posted >= %(crit_date)s::date)
        AND (NOT %(crit_has_excl)s OR NOT EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN locations l ON l.text = btrim(loc)
              JOIN locations x ON x.text = ANY(%(crit_excl)s::text[])
              WHERE {_PLACE_MATCH}))
        AND (NOT %(crit_has_incl)s OR cardinality(j.locations) = 0 OR EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN locations l ON l.text = btrim(loc)
              JOIN locations x ON x.text = ANY(%(crit_incl)s::text[])
              WHERE {_PLACE_MATCH}))
"""


def params(settings_row: dict[str, Any] | None) -> dict[str, Any]:
    crit = (settings_row or {}).get("criteria") or {}
    excl = [s.strip() for s in crit.get("excluded_locations", []) if s.strip()]
    incl = [s.strip() for s in crit.get("included_locations", []) if s.strip()]
    return {
        "crit_date": crit.get("date_posted_after"),
        "crit_excl": excl,
        "crit_has_excl": bool(excl),
        "crit_incl": incl,
        "crit_has_incl": bool(incl),
    }
