from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_now = text("now()")


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy's declarative API
        datetime.datetime: TIMESTAMP(timezone=True),
        list[str]: ARRAY(Text),
        dict: JSONB,
    }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sub: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    groups: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class GroupBudget(Base):
    __tablename__ = "group_budgets"

    group_name: Mapped[str] = mapped_column(Text, primary_key=True)
    weekly_token_budget: Mapped[int | None] = mapped_column(BigInteger)
    # NULL = default policy (unlimited tiers: every server-keyed model;
    # budgeted tiers: the env allowlist). A list = exactly these models.
    allowed_models: Mapped[list[str] | None] = mapped_column(ARRAY(Text))


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
    locations: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    terms: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    source: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    date_posted: Mapped[datetime.datetime | None]
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    extraction_status: Mapped[str | None] = mapped_column(Text)
    comp_min: Mapped[int | None] = mapped_column(BigInteger)
    comp_max: Mapped[int | None] = mapped_column(BigInteger)
    comp_text: Mapped[str | None] = mapped_column(Text)
    comp_period: Mapped[str | None] = mapped_column(Text)
    comp_currency: Mapped[str | None] = mapped_column(Text)
    comp_basis: Mapped[str | None] = mapped_column(Text)
    comp_extracted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserJob(Base):
    __tablename__ = "user_jobs"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str | None] = mapped_column(Text)
    date_applied: Mapped[datetime.date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    size: Mapped[str | None] = mapped_column(Text)
    recruiter: Mapped[str | None] = mapped_column(Text)
    connection1: Mapped[str | None] = mapped_column(Text)
    connection2: Mapped[str | None] = mapped_column(Text)
    documents: Mapped[str | None] = mapped_column(Text)
    hidden: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserJobHistory(Base):
    __tablename__ = "user_job_history"
    __table_args__ = (Index("idx_user_job_history_row", "user_id", "job_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    old_status: Mapped[str | None] = mapped_column(Text)
    new_status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


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
    members: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
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
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
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
    column_layout: Mapped[Any | None] = mapped_column(JSONB)
    prefs: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    api_key_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    bypass_sponsorship_filter: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    criteria: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    ai_provider: Mapped[str] = mapped_column(Text, server_default=text("'openai'"))
    ai_base_url: Mapped[str | None] = mapped_column(Text)
    ai_model: Mapped[str | None] = mapped_column(Text)
    ai_params: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    email_digest: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    digest_token: Mapped[str | None] = mapped_column(Text, unique=True)
    last_digest_at: Mapped[datetime.datetime | None]
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    __table_args__ = (Index("idx_api_usage_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    key_source: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_status", "status", "id"),
        Index(
            "idx_tasks_parent",
            "parent_id",
            "status",
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    parent_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    worker: Mapped[str | None] = mapped_column(Text)
    last_heartbeat: Mapped[datetime.datetime | None]
    progress: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    started_at: Mapped[datetime.datetime | None]
    finished_at: Mapped[datetime.datetime | None]


class AiBatch(Base):
    __tablename__ = "ai_batches"
    __table_args__ = (
        Index("idx_ai_batches_status", "status", "id"),
        Index("idx_ai_batches_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider_batch_id: Mapped[str] = mapped_column(Text, unique=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(Text, server_default=text("''"))
    model: Mapped[str | None] = mapped_column(Text)
    requests: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    completed: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'submitted'"))
    est_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    est_cost_usd: Mapped[Any | None] = mapped_column(Numeric(12, 6))
    submitted_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    completed_at: Mapped[datetime.datetime | None]


class WorkerStatus(Base):
    __tablename__ = "worker_status"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    current_task_id: Mapped[int | None] = mapped_column(BigInteger)


class HealthAlert(Base):
    __tablename__ = "health_alerts"
    __table_args__ = (
        Index(
            "uq_health_alerts_open",
            "kind",
            "subject",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, server_default=text("'warning'"))
    message: Mapped[str] = mapped_column(Text, server_default=text("''"))
    detail: Mapped[Any | None] = mapped_column(JSONB)
    first_seen: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    notified_at: Mapped[datetime.datetime | None]
    resolved_at: Mapped[datetime.datetime | None]


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
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    resolved_at: Mapped[datetime.datetime | None]


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("idx_reports_status", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, server_default=text("''"))
    corrections: Mapped[Any | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    resolved_at: Mapped[datetime.datetime | None]
