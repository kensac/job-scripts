"""Posting path response shapes."""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel

Outcome = Literal["passed", "failed", "skipped", "pending", "info"]

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
