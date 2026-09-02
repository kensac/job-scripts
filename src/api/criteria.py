from __future__ import annotations

import re
from typing import Any

# Structured per-user criteria applied to source-derived visibility and to
# filter-run candidacy (so AI never spends on jobs a user excludes outright).
# Param-guarded so the clauses collapse to TRUE when a criterion is unset.
# Location exclusions match on word boundaries, not bare substrings:
# "UK" must exclude "London, UK" but not "Tukwila, WA".
SQL = """
        AND (%(crit_date)s::date IS NULL OR j.date_posted >= %(crit_date)s::date)
        AND (NOT %(crit_has_excl)s OR NOT EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN unnest(%(crit_excl)s::text[]) ex ON lower(loc) ~ ('\\m' || ex || '\\M')))
"""


def params(settings_row: dict[str, Any] | None) -> dict[str, Any]:
    crit = (settings_row or {}).get("criteria") or {}
    excl = [
        re.escape(s.strip().lower())
        for s in crit.get("excluded_locations", [])
        if s.strip()
    ]
    return {
        "crit_date": crit.get("date_posted_after"),
        "crit_excl": excl,
        "crit_has_excl": bool(excl),
    }
