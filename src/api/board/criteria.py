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
# The place rule, spelled once in two parts and composed below into both
# shapes a caller can evaluate. A criterion entry x matches a location l when
# x is a bare remote criterion and l is remote, or when one of x's places, its
# null levels dropped, is contained in l's places: every level the entry names
# is equal. Every stored place carries a string country with region and city
# beside it (2026-11-26); a place with no country never matches, as an equal
# comparison with nothing never did.
_REMOTE_MATCH = "(x.country IS NULL AND x.remote AND l.remote)"
_PLACE_NAMED = "xp->>'country' IS NOT NULL"
_PLACE_CONTAINS = "l.places @> jsonb_build_array(jsonb_strip_nulls(xp))"
_PLACE_MATCH = f"""({_REMOTE_MATCH} OR EXISTS (
                  SELECT 1 FROM jsonb_array_elements(x.places) xp
                  WHERE {_PLACE_NAMED} AND {_PLACE_CONTAINS}))"""

# Per posting: the posting's location strings joined to `locations`, each
# checked against the criterion. The shape for a criterion that differs per
# row (json_sql) and for one posting at a time (api.posting_path).
_PER_POSTING = {
    "excl": f"""NOT EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN locations l ON l.text = btrim(loc)
              JOIN locations x ON x.text = ANY(%(crit_excl)s::text[])
              WHERE {_PLACE_MATCH})""",
    "incl": f"""EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN locations l ON l.text = btrim(loc)
              JOIN locations x ON x.text = ANY(%(crit_incl)s::text[])
              WHERE {_PLACE_MATCH})""",
}

# Per statement: every location string the criterion matches, found once and
# hashed, then probed per posting location. The shape for a scan of many
# postings under one criterion (SQL). Per posting, a person's board read
# re-matched the criterion about 460,000 times twice over: 20 of its 26
# seconds on a production copy (2026-11-26). The containment half reads
# idx_locations_places rather than every location. `IS TRUE` keeps the set a
# hashed subplan; as a bare IN the planner pulls it into a per-posting join.
_MATCHING = f"""
              SELECT l.text FROM locations x
              CROSS JOIN LATERAL jsonb_array_elements(x.places) xp
              JOIN locations l ON {_PLACE_CONTAINS}
              WHERE x.text = ANY(%(crit_{{key}})s::text[]) AND {_PLACE_NAMED}
              UNION
              SELECT l.text FROM locations x JOIN locations l ON {_REMOTE_MATCH}
              WHERE x.text = ANY(%(crit_{{key}})s::text[])"""
_PER_STATEMENT = {
    key: f"""{"NOT " if key == "excl" else ""}EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              WHERE (btrim(loc) IN ({_MATCHING.format(key=key)})) IS TRUE)"""
    for key in ("excl", "incl")
}


def _conditions(locations: dict[str, str]) -> dict[str, str]:
    return {
        "posted_after": "(%(crit_date)s::date IS NULL OR j.date_posted >= %(crit_date)s::date)",
        "max_age": """(%(crit_max_age)s::int IS NULL
             OR COALESCE(j.date_posted, j.created_at)
                >= now() - make_interval(days => %(crit_max_age)s::int))""",
        "excluded_locations": f"(NOT %(crit_has_excl)s OR {locations['excl']})",
        "included_locations": (
            f"(NOT %(crit_has_incl)s OR cardinality(j.locations) = 0 OR {locations['incl']})"
        ),
        "terms": "(NOT %(crit_has_terms)s OR j.terms && %(crit_terms)s::text[])",
    }


# Each criterion on its own, named, so a posting's path can say which one it
# failed (api.posting_path), in the per-posting shape that json_sql also binds
# to stored criteria. SQL below is the same conjunction for a scan.
CONDITIONS: dict[str, str] = _conditions(_PER_POSTING)

# The conjunction for a scan of many postings under one person's or board's
# criteria. The same conditions, the locations in their per-statement shape.
SQL = "".join(f"\n        AND {c}" for c in _conditions(_PER_STATEMENT).values()) + "\n"


def json_sql(criteria: str, names: tuple[str, ...] = tuple(CONDITIONS)) -> str:
    """Bind the same predicate to stored criteria instead of one user's params.

    Only trusted SQL expressions belong here. Values remain in the database,
    and list normalization matches params(), including blank entries.
    `names` picks some of the CONDITIONS, for a caller that runs them in
    stages (api.verification_candidates.reachable).
    """
    bindings = {
        "crit_date": f"({criteria}->>'date_posted_after')",
        "crit_max_age": f"({criteria}->>'max_age_days')",
    }
    for suffix, field in (
        ("excl", "excluded_locations"),
        ("incl", "included_locations"),
        ("terms", "included_terms"),
    ):
        values = (
            f"ARRAY(SELECT btrim(value) FROM jsonb_array_elements_text("
            f"COALESCE({criteria}->'{field}', '[]'::jsonb)) AS entry(value) "
            "WHERE btrim(value) <> '')"
        )
        bindings[f"crit_{suffix}"] = values
        bindings[f"crit_has_{suffix}"] = f"(cardinality({values}) > 0)"
    sql = "".join(f"\n        AND {CONDITIONS[name]}" for name in names) + "\n"
    return sql % {key: value for key, value in bindings.items()}


def params(settings_row: dict[str, Any] | None) -> dict[str, Any]:
    crit = (settings_row or {}).get("criteria") or {}
    excl = [s.strip() for s in crit.get("excluded_locations", []) if s.strip()]
    incl = [s.strip() for s in crit.get("included_locations", []) if s.strip()]
    terms = [s.strip() for s in crit.get("included_terms", []) if s.strip()]
    return {
        "crit_date": crit.get("date_posted_after"),
        "crit_max_age": crit.get("max_age_days"),
        "crit_excl": excl,
        "crit_has_excl": bool(excl),
        "crit_incl": incl,
        "crit_has_incl": bool(incl),
        "crit_terms": terms,
        "crit_has_terms": bool(terms),
    }
