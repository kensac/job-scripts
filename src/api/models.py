from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class UserJobPatch(BaseModel):
    status: Optional[str] = None
    date_applied: Optional[datetime.date] = None
    notes: Optional[str] = None
    size: Optional[str] = None
    recruiter: Optional[str] = None
    connection1: Optional[str] = None
    connection2: Optional[str] = None
    documents: Optional[str] = None
    hidden: Optional[bool] = None


class UploadRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=50)


class FilterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=8000)
    on_ambiguous: str = "keep"
    fail_closed: bool = False
    enabled: bool = True


class FilterPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=8000)
    on_ambiguous: Optional[str] = None
    fail_closed: Optional[bool] = None
    enabled: Optional[bool] = None


class ImprovePromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


class SourcesPut(BaseModel):
    enabled: List[str]


class SettingsPut(BaseModel):
    column_layout: Optional[Any] = None
    prefs: Optional[Dict[str, Any]] = None
    ai_model: Optional[str] = Field(default=None, max_length=200)
    ai_params: Optional[Dict[str, Any]] = None


class ApiKeyPut(BaseModel):
    api_key: str = Field(min_length=8, max_length=400)
    provider: str = "openai"
    base_url: Optional[str] = Field(default=None, max_length=400)
