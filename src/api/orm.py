from __future__ import annotations

import datetime
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_now = text("now()")


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime.datetime: TIMESTAMP(timezone=True),
        List[str]: ARRAY(Text),
        dict: JSONB,
    }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sub: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[Optional[str]] = mapped_column(Text)
    name: Mapped[Optional[str]] = mapped_column(Text)
    groups: Mapped[List[str]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class GroupBudget(Base):
    __tablename__ = "group_budgets"

    group_name: Mapped[str] = mapped_column(Text, primary_key=True)
    weekly_token_budget: Mapped[Optional[int]] = mapped_column(BigInteger)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_source", "source"),
        Index("idx_jobs_uploaded_by", "uploaded_by"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    raw_url: Mapped[str] = mapped_column(Text, server_default=text("''"))
    company: Mapped[str] = mapped_column(Text, server_default=text("''"))
    title: Mapped[str] = mapped_column(Text, server_default=text("''"))
    locations: Mapped[List[str]] = mapped_column(server_default=text("'{}'"))
    terms: Mapped[List[str]] = mapped_column(server_default=text("'{}'"))
    source: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    date_posted: Mapped[Optional[datetime.datetime]]
    uploaded_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id")
    )
    extraction_status: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserJob(Base):
    __tablename__ = "user_jobs"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[Optional[str]] = mapped_column(Text)
    date_applied: Mapped[Optional[datetime.date]] = mapped_column(Date)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    size: Mapped[Optional[str]] = mapped_column(Text)
    recruiter: Mapped[Optional[str]] = mapped_column(Text)
    connection1: Mapped[Optional[str]] = mapped_column(Text)
    connection2: Mapped[Optional[str]] = mapped_column(Text)
    documents: Mapped[Optional[str]] = mapped_column(Text)
    hidden: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class Source(Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    listings_url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class SourceGroup(Base):
    __tablename__ = "source_groups"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    members: Mapped[List[str]] = mapped_column(server_default=text("'{}'"))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserSource(Base):
    __tablename__ = "user_sources"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(Text, primary_key=True)


class UserFilter(Base):
    __tablename__ = "user_filters"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    on_ambiguous: Mapped[str] = mapped_column(Text, server_default=text("'keep'"))
    fail_closed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    prompt_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    column_layout: Mapped[Optional[Any]] = mapped_column(JSONB)
    prefs: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    api_key_enc: Mapped[Optional[bytes]] = mapped_column(BYTEA)
    bypass_sponsorship_filter: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true")
    )
    ai_provider: Mapped[str] = mapped_column(Text, server_default=text("'openai'"))
    ai_base_url: Mapped[Optional[str]] = mapped_column(Text)
    ai_model: Mapped[Optional[str]] = mapped_column(Text)
    ai_params: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    __table_args__ = (Index("idx_api_usage_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    key_source: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("idx_tasks_status", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    dedupe_key: Mapped[Optional[str]] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    last_heartbeat: Mapped[Optional[datetime.datetime]]
    progress: Mapped[Optional[Any]] = mapped_column(JSONB)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    started_at: Mapped[Optional[datetime.datetime]]
    finished_at: Mapped[Optional[datetime.datetime]]


class FilterPreset(Base):
    __tablename__ = "filter_presets"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    prompt: Mapped[str] = mapped_column(Text)
    on_ambiguous: Mapped[str] = mapped_column(Text, server_default=text("'keep'"))
    fail_closed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class SourceRequest(Base):
    __tablename__ = "source_requests"
    __table_args__ = (Index("idx_source_requests_status", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    url: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolution_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    resolved_at: Mapped[Optional[datetime.datetime]]


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("idx_reports_status", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, server_default=text("''"))
    corrections: Mapped[Optional[Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolution_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    resolved_at: Mapped[Optional[datetime.datetime]]
