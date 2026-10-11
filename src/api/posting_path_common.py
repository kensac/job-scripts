"""Posting path response shapes and shared verdict provenance."""

from __future__ import annotations

import contextvars
import datetime
from typing import Any, Literal

from pydantic import BaseModel

from api import db
from core import verdict_reads

Outcome = Literal["passed", "failed", "skipped", "pending", "info"]

# Each board and filter asks for the same closed and clearance verdicts; one
# path reads them once.
_LATEST: contextvars.ContextVar[dict[tuple[str, str], Any]] = contextvars.ContextVar("path_latest")
Basis = Literal["recorded", "evaluated_now"]


class PathStep(BaseModel):
    stage: str
    outcome: Outcome
    label: str
    detail: str | None = None
    basis: Basis
    at: datetime.datetime | None = None
    # What an action on this step addresses (re-check, explain).
    check: str | None = None
    prompt_hash: str | None = None
    filter_id: int | None = None


class ConsumerPath(BaseModel):
    kind: Literal["managed_board", "filter", "board"]
    id: int | None
    name: str
    owner: str | None = None
    included: bool | None
    summary: str
    steps: list[PathStep]


class PostingPath(BaseModel):
    job_id: int
    url: str
    title: str | None
    source: str
    steps: list[PathStep]
    consumers: list[ConsumerPath]


# Where a verdict row came from, by config_name, in the words the drawer uses.
_ORIGIN = {
    "verify-batch": "answered when the posting was verified",
    "verify-near-copy": "copied from a twin posting",
    "reverify": "re-checked",
    "reverify-unchanged": "re-check reused: the page had not changed",
    "admin": "set by an admin",
    "filter-batch": "judged by its own run",
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
