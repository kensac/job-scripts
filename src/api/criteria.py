from __future__ import annotations

import re
from typing import Any

# jobs.comp_min is bigint, so this is the column's domain, not a judgement
# about salaries. It exists to make an out-of-range bar a 422 rather than a
# 500 at parameter-adaptation time; Python ints are unbounded and psycopg
# cannot adapt one that does not fit. No plausibility ceiling is defensible
# here - a bar above the highest advertised figure simply matches nothing that
# publishes pay, which is a legible outcome rather than an invalid input.
COMP_MAX = 2**63 - 1

# Structured per-user criteria applied to source-derived visibility and to
# filter-run candidacy (so AI never spends on jobs a user excludes outright).
# Param-guarded so the clauses collapse to TRUE when a criterion is unset.
# Location matches use word boundaries, not bare substrings: "UK" must exclude
# "London, UK" but not "Tukwila, WA".
#
# comp_min keeps rows whose comp_min IS NULL, and that is the whole design:
# only 46% of active jobs publish a floor (9,929 of 21,534, read 2026-09-03),
# so a bare `>=` would hide the 54% that price nothing rather than the ones
# paying badly. The bar means "hide jobs advertising less than this", which is
# a weaker promise than "only show jobs above this" and has to be described
# that way wherever it is offered.
SQL = """
        AND (%(crit_date)s::date IS NULL OR j.date_posted >= %(crit_date)s::date)
        AND (NOT %(crit_has_excl)s OR NOT EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN unnest(%(crit_excl)s::text[]) ex ON lower(loc) ~ ('\\m' || ex || '\\M')))
        AND (NOT %(crit_has_incl)s OR EXISTS (
              SELECT 1 FROM unnest(j.locations) loc
              JOIN unnest(%(crit_incl)s::text[]) inc ON lower(loc) ~ ('\\m' || inc || '\\M')))
        AND (%(crit_comp_min)s::bigint IS NULL OR j.comp_min IS NULL
             OR j.comp_min >= %(crit_comp_min)s::bigint)
"""


def _terms(values: Any) -> list[str]:
    return [re.escape(s.strip().lower()) for s in values or [] if s and s.strip()]


def params(settings_row: dict[str, Any] | None) -> dict[str, Any]:
    crit = (settings_row or {}).get("criteria") or {}
    excl = _terms(crit.get("excluded_locations"))
    incl = _terms(crit.get("included_locations"))
    return {
        "crit_date": crit.get("date_posted_after"),
        "crit_excl": excl,
        "crit_has_excl": bool(excl),
        "crit_incl": incl,
        "crit_has_incl": bool(incl),
        "crit_comp_min": crit.get("comp_min"),
    }
