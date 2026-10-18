"""Verdict origin and cached reads for one posting path."""

from __future__ import annotations

import contextvars
from typing import Any

from api import db
from core import verdict_reads

# One path reads each closed and clearance verdict once.
_LATEST: contextvars.ContextVar[dict[tuple[str, str], Any]] = contextvars.ContextVar("path_latest")

# Where a verdict row came from, by config_name, in the words the drawer uses.
_ORIGIN = {
    "verify-batch": "answered when the posting was verified",
    "verify-near-copy": "copied from a twin posting",
    "filter-near-copy": "copied from a twin posting",
    "reverify": "re-checked",
    "reverify-unchanged": "re-check reused: the page had not changed",
    "admin": "set by an admin",
    "filter-batch": "judged by its own run",
    "filter-pay-screen": "settled from the stated pay, no model call",
    "explain": "asked by a person",
}


def _verdict_origin(config_name: str | None) -> str:
    return _ORIGIN.get(config_name or "", (config_name or "recorded").replace("-", " "))


def _latest_verdict(url: str, check: str) -> verdict_reads.LatestVerdict | None:
    key = (url, check)
    if key not in _LATEST.get():
        _LATEST.get()[key] = verdict_reads.read_latest(url, check)
    return _LATEST.get()[key]


def _near_copy_source_url(job: dict[str, Any]) -> str | None:
    """The posting a near-copy verdict was copied from, found again by its key."""
    if not job["near_copy_key"]:
        return None
    row = db.query_one(
        "SELECT j.url FROM jobs j WHERE j.source = %s AND j.near_copy_key = %s AND j.id <> %s "
        "AND EXISTS (SELECT 1 FROM ai_queries q WHERE q.url = j.url AND q.check_type = 'closed' "
        "AND q.config_name IS DISTINCT FROM 'verify-near-copy') ORDER BY j.id LIMIT 1",
        (job["source"], job["near_copy_key"], job["id"]),
    )
    return row["url"] if row else None
