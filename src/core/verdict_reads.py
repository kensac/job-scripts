"""Shared SQL builders for reading decided answers from verdicts.

Callers pass SQL expressions for URL and prompt hash. Named checks are validated
against the registry before becoming SQL literals; custom checks use prompt_hash."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Literal, LiteralString, cast

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
    """Latest answer to `check` for one posting as a subquery body (`vlast` alias
    avoids outer column capture)."""
    where = f"vlast.url = {url} AND vlast.check_type = {_check(check)}"
    if prompt_hash is not None:
        where += f" AND vlast.prompt_hash = {prompt_hash}"
    if model is not None:
        where += f" AND vlast.model = {model}"
    return f"SELECT {columns} FROM verdicts vlast WHERE {where} ORDER BY vlast.id DESC LIMIT 1"


def latest_per(key: str, columns: str, where: str, *, alias: str = "v", join: str = "") -> str:
    """Latest answer per `key` via DISTINCT ON ordered by `(key, id DESC)`.

    Keying on integer `jobs.id` avoids text sorting (245 ms vs 1.15 s in
    api/analytics/queries.py).
    """
    return (
        f"SELECT DISTINCT ON ({key}) {columns} FROM verdicts {alias} {join} "
        f"WHERE {where} ORDER BY {key}, {alias}.id DESC"
    )


def latest_status(url: str, check: str, *, prompt_hash: str | None = None) -> str:
    """Latest status ('passed' or 'rejected'), or NULL if unchecked."""
    return f"({latest(url, check, prompt_hash=prompt_hash)})"


def posting_status(job: str, check: str) -> str:
    """`latest_status` for catalog scans, reading `latest_posting_checks` unless
    `latest_posting_checks_dirty` flags the URL for a live `verdicts` fallback.

    Correlated dirty probe stays an index probe under backlog. Measured on a
    production copy (2026-11-26, 315,390 candidates): 5.0 s vs 16.7 s probing
    ai_queries directly.
    """
    if check == "custom":
        raise ValueError("custom answers are keyed by prompt; use latest_status")
    stored = (
        f"(SELECT lpc.status FROM latest_posting_checks lpc "
        f"WHERE lpc.job_id = {job}.id AND lpc.check_type = {_check(check)})"
    )
    return (
        f"(CASE WHEN EXISTS (SELECT 1 FROM latest_posting_checks_dirty lpcd "
        f"WHERE lpcd.url = {job}.url) "
        f"THEN {latest_status(f'{job}.url', check)} ELSE {stored} END)"
    )


def closed_verdict(url: str) -> str:
    """Latest closed check mapped to 'open', 'closed', or NULL when unchecked."""
    shown = "CASE vlast.status WHEN 'passed' THEN 'open' WHEN 'rejected' THEN 'closed' END"
    return f"({latest(url, 'closed', shown)})"


# Three-valued with NULL ("never checked"); never collapse NULL into 'closed'.
Openness = Literal["open", "closed"]


def has_verdict(url: str, check: str) -> str:
    """Whether any answer exists for `check` (takes no status; use `latest_status`
    for current pass/reject state)."""
    return (
        f"EXISTS (SELECT 1 FROM verdicts vany "
        f"WHERE vany.url = {url} AND vany.check_type = {_check(check)})"
    )


def verified_open(job: str) -> str:
    """True when the posting's latest closed and clearance verdicts both passed."""
    return (
        f"{posting_status(job, 'closed')} = 'passed' "
        f"AND {posting_status(job, 'clearance')} = 'passed'"
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
    """Latest answer to `check` for one posting, optionally scoped to `model`."""
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
    """Latest answer to each of `checks` per posting, keyed as `{url: {check: verdict}}`."""
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
