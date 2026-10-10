"""The shapes a decided answer is read in, written once.

`verdicts` says which ai_queries rows are answers. This says how a reader asks
of them: the latest answer to a check, whether an answer exists. Each shape
was hand-written in several modules, so "latest" (highest id) and "what a
closed answer shows a person" were restated per reader and free to drift.
`tests/test_verdicts_view.py` fails when a module outside this one writes a
latest-answer shape against the view.

The SQL builders take SQL expressions from the caller (`url="j.url"`,
`prompt_hash="e.prompt_hash"`). A check is a name, checked against the
registry and inlined as a literal, never a caller's value.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import LiteralString, cast

from core.checks import POSTING_CHECKS
from core.pool import connection


def _check(check: str) -> str:
    if check != "custom" and check not in POSTING_CHECKS:
        raise ValueError(f"unknown check {check!r}")
    return f"'{check}'"


def latest(
    url: str,
    check: str,
    columns: str = "status",
    *,
    prompt_hash: str | None = None,
    model: str | None = None,
) -> str:
    """The latest answer to `check` for one posting, as a subquery body: wrap
    it in parentheses for a value, or join it LATERAL for several columns.

    `vlast` is the alias so an unqualified column inside cannot be captured
    by an outer table of the same name."""
    where = f"vlast.url = {url} AND vlast.check_type = {_check(check)}"
    if prompt_hash is not None:
        where += f" AND vlast.prompt_hash = {prompt_hash}"
    if model is not None:
        where += f" AND vlast.model = {model}"
    return f"SELECT {columns} FROM verdicts vlast WHERE {where} ORDER BY vlast.id DESC LIMIT 1"


def latest_per(key: str, columns: str, where: str, *, alias: str = "v", join: str = "") -> str:
    """The latest answer per `key` (one row each), as a statement: a CTE
    body, a derived table, or a query on its own.

    The ORDER BY is the key then newest id, so an index on (url, check_type,
    id) or (url, prompt_hash, id) yields rows already in order. A key on
    jobs.id (`join="JOIN jobs j ON j.url = v.url"`) sorts integers where a
    url key sorts text: api/routers/analytics.py measured 1.15 s against
    245 ms for the same rows."""
    return (
        f"SELECT DISTINCT ON ({key}) {columns} FROM verdicts {alias} {join} "
        f"WHERE {where} ORDER BY {key}, {alias}.id DESC"
    )


def latest_status(url: str, check: str, *, prompt_hash: str | None = None) -> str:
    """The latest answer's status ('passed' or 'rejected'), NULL when none."""
    return f"({latest(url, check, prompt_hash=prompt_hash)})"


def closed_verdict(url: str) -> str:
    """What a posting's latest closed answer shows a person: 'open', 'closed',
    or NULL when nothing has looked. The board row, a public list card and
    the job page all show this one value."""
    shown = "CASE vlast.status WHEN 'passed' THEN 'open' WHEN 'rejected' THEN 'closed' END"
    return f"({latest(url, 'closed', shown)})"


def has_verdict(url: str, check: str) -> str:
    """EXISTS an answer to `check`: the check has been answered at all.

    It takes no status on purpose. "Ever rejected" and "ever passed" read as
    the latest answer and were not: the content sweep skipped 99 active
    postings rejected as closed once and open since, and the full re-check
    re-ran 214 passed once and closed since (production, 2026-10-10). Ask
    `latest_status` for what a posting is now."""
    return (
        f"EXISTS (SELECT 1 FROM verdicts vany "
        f"WHERE vany.url = {url} AND vany.check_type = {_check(check)})"
    )


# A posting whose latest closed and clearance verdicts both passed. The
# extractors (comp, requirements) select on it: on 2026-09-06 the catalog
# held 74,477 active postings of which 37,438 were verified open, and both
# extractors were paying for the other half, whose numbers nothing reads
# because a closed or restricted posting reaches no board. The narrower
# option, extracting only for postings on someone's board (3,117 that day,
# 4 percent), is not taken yet: a posting reaching a board later would wait
# a cycle for its comp column, and the market table would be built from a
# smaller slice than the filters admit. Written down here so it is a
# decision and not an oversight.
#
# A retention policy on ai_queries (none exists; it is the largest table and
# the decision is open) must keep the latest verdict per (url, check_type),
# or a posting whose verdicts age out silently reads as unverified here and
# drops out of both extractors, then re-enters them at cost once re-verified.
def verified_open(url: str) -> str:
    return (
        f"{latest_status(url, 'closed')} = 'passed' "
        f"AND {latest_status(url, 'clearance')} = 'passed'"
    )


@dataclass(frozen=True)
class LatestVerdict:
    status: str
    reason: str | None
    config_name: str | None
    model: str | None
    created_at: datetime.datetime
    request_sha256: str | None


_COLUMNS = "status, reason, config_name, model, created_at, request_sha256"


def read_latest(
    url: str, check: str, *, prompt_hash: str | None = None, model: str | None = None
) -> LatestVerdict | None:
    """The latest answer to `check` for one posting, or None when nothing
    has answered it. With `model`, only that model's answers count."""
    sql = latest(
        "%(url)s",
        check,
        _COLUMNS,
        prompt_hash=None if prompt_hash is None else "%(prompt_hash)s",
        model=None if model is None else "%(model)s",
    )
    params = {"url": url, "prompt_hash": prompt_hash, "model": model}
    with connection() as conn:
        row = conn.execute(cast("LiteralString", sql), params).fetchone()
    return LatestVerdict(**row) if row else None


def latest_checks(
    urls: list[str], checks: tuple[str, ...] = ("closed", "clearance")
) -> dict[str, dict[str, LatestVerdict]]:
    """The latest answer to each of `checks` per posting, as url -> check ->
    answer. A url or check nothing has answered is absent."""
    sql = latest_per(
        "url, check_type",
        f"url, check_type, {_COLUMNS}",
        f"url = ANY(%s) AND check_type IN ({', '.join(_check(c) for c in checks)})",
    )
    out: dict[str, dict[str, LatestVerdict]] = {}
    with connection() as conn:
        for row in conn.execute(cast("LiteralString", sql), (urls,)).fetchall():
            url, check = row.pop("url"), row.pop("check_type")
            out.setdefault(url, {})[check] = LatestVerdict(**row)
    return out
