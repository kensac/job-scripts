"""Multi-column sort from query parameters, shared by every list endpoint.

`sort=company,date_posted&dir=asc,desc` orders by company ascending, then
date_posted descending; a `dir` shorter than `sort` repeats its last value,
so `sort=a,b&dir=asc` is ascending on both. Keys outside the endpoint's
whitelist are dropped rather than refused, and an empty result falls back
to the endpoint's default, so an old client's single `sort` keeps working
and a typo never 500s. The echo (`sorts`) is what the UI renders as the
active sort; it never has to duplicate the default or guess the keys.
"""

from __future__ import annotations


def parse(sort: str, dir: str, sortable: dict[str, str], default: str) -> list[dict[str, str]]:
    keys = [k.strip() for k in sort.split(",")]
    dirs = [d.strip().lower() for d in dir.split(",")] or ["desc"]
    sorts = [
        {"key": k, "dir": "asc" if dirs[min(i, len(dirs) - 1)] == "asc" else "desc"}
        for i, k in enumerate(keys)
        if k in sortable
    ]
    return sorts or [{"key": default, "dir": "asc" if dirs[0] == "asc" else "desc"}]


def clause(sorts: list[dict[str, str]], sortable: dict[str, str]) -> str:
    """The ORDER BY body, every column NULLS LAST so empty cells sink whichever
    way the column runs. Callers append their own tiebreaker."""
    return ", ".join(f"{sortable[s['key']]} {s['dir'].upper()} NULLS LAST" for s in sorts)
