from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserJobPatch(BaseModel):
    status: str | None = None
    date_applied: datetime.date | None = None
    notes: str | None = None
    size: str | None = None
    recruiter: str | None = None
    connection1: str | None = None
    connection2: str | None = None
    documents: str | None = None
    hidden: bool | None = None


class UserJobsBulkPatch(BaseModel):
    """One patch for a selection. Same field vocabulary as UserJobPatch; the
    cap keeps one request from holding a transaction over a whole catalog."""

    job_ids: list[int] = Field(min_length=1, max_length=5000)
    patch: UserJobPatch


class UserJobsBulkIds(BaseModel):
    job_ids: list[int] = Field(min_length=1, max_length=5000)


class UploadRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50)


class FilterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=8000)
    on_ambiguous: str = "keep"
    fail_closed: bool = False
    enabled: bool = True


class FilterPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    on_ambiguous: str | None = None
    fail_closed: bool | None = None
    enabled: bool | None = None


class ImprovePromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


class SourcesPut(BaseModel):
    enabled: list[str]


class Criteria(BaseModel):
    date_posted_after: datetime.date | None = None
    excluded_locations: list[str] = Field(default_factory=list, max_length=100)
    # Places a posting must be in to be shown at all; empty means anywhere.
    included_locations: list[str] = Field(default_factory=list, max_length=100)


class SettingsPut(BaseModel):
    """Rejects unknown keys rather than dropping them.

    `background` was a key here until it was removed, and a client still
    sending it needs to be told. Pydantic's default is to discard an
    unrecognised key silently, which would answer 200 to a write that
    stored nothing - the one failure a caller cannot detect.
    """

    model_config = ConfigDict(extra="forbid")

    column_layout: Any | None = None
    prefs: dict[str, Any] | None = None
    ai_model: str | None = Field(default=None, max_length=200)
    ai_params: dict[str, Any] | None = None
    bypass_sponsorship_filter: bool | None = None
    criteria: Criteria | None = None
    email_digest: bool | None = None


class ApiKeyPut(BaseModel):
    api_key: str = Field(min_length=8, max_length=400)
    provider: str = "openai"
    base_url: str | None = Field(default=None, max_length=400)
