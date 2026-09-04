from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from api.criteria import COMP_MAX
from core.requirements import CLEARANCE_LEVELS, DEGREE_LEVELS, MAX_PLAUSIBLE_YOE, in_vocabulary


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
    included_locations: list[str] = Field(default_factory=list, max_length=100)
    comp_min: int | None = Field(default=None, ge=0, le=COMP_MAX)


def _checked(value: str | None, allowed: tuple[str, ...], field: str) -> str | None:
    """Rejects rather than silently drops an unknown level.

    core.requirements.in_vocabulary answers None for anything outside the
    vocabulary, which is right for a model's answer - there is nobody to tell.
    A user typing their own degree is different: silently storing None would
    show them a gap analysis measured against a background they did not give.
    """
    if value is None or not value.strip():
        return None
    token = in_vocabulary(value, allowed)
    if token is None:
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
    return token


class Background(BaseModel):
    """What the user says they bring, in the same vocabulary the extraction
    writes, so the gap query compares like with like instead of guessing at a
    free-text resume. Every field is optional: an unset field means "do not
    measure me against this", which is a different answer from zero.
    """

    yoe: int | None = Field(default=None, ge=0, le=MAX_PLAUSIBLE_YOE)
    degree: str | None = None
    degree_fields: list[str] = Field(default_factory=list, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=200)
    clearance: str | None = None
    citizen: bool | None = None
    needs_sponsorship: bool | None = None

    @field_validator("degree")
    @classmethod
    def _degree(cls, v: str | None) -> str | None:
        return _checked(v, DEGREE_LEVELS, "degree")

    @field_validator("clearance")
    @classmethod
    def _clearance(cls, v: str | None) -> str | None:
        return _checked(v, CLEARANCE_LEVELS, "clearance")


class SettingsPut(BaseModel):
    column_layout: Any | None = None
    prefs: dict[str, Any] | None = None
    ai_model: str | None = Field(default=None, max_length=200)
    ai_params: dict[str, Any] | None = None
    bypass_sponsorship_filter: bool | None = None
    criteria: Criteria | None = None
    background: Background | None = None
    email_digest: bool | None = None


class ApiKeyPut(BaseModel):
    api_key: str = Field(min_length=8, max_length=400)
    provider: str = "openai"
    base_url: str | None = Field(default=None, max_length=400)
