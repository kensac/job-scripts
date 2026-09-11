"""People, configuration, and the work the fleet runs."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Identity,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


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


class HostBudget(Base):
    """The pace one egress address keeps against one upstream host, learned
    from refusals. See api.hosts."""

    __tablename__ = "host_budget"

    host: Mapped[str] = mapped_column(Text, primary_key=True)
    egress_group: Mapped[str] = mapped_column(Text, primary_key=True)
    pace_seconds: Mapped[float] = mapped_column(Float, server_default=text("0"))
    next_allowed_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    ok: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    refused: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
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
    # How the person writes, in their own words; NULL means the built-in
    # default in tasks.application.
    writing_style: Mapped[str | None] = mapped_column(Text)
    # The facts every application form asks, in api.apply.Profile's shape.
    profile: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    digest_token: Mapped[str | None] = mapped_column(Text, unique=True)
    last_digest_at: Mapped[datetime.datetime | None]
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


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
        # Every worker asks once a minute, per active source, whether that
        # source has a pending ingest and when its last one was. Without this
        # each of those is a sequential scan of tasks: 738 scans and 1.4s per
        # tick per worker on 2026-09-04, measured with EXPLAIN ANALYZE.
        Index(
            "idx_tasks_ingest_source",
            text("(payload->>'source')"),
            text("created_at DESC"),
            postgresql_where=text("kind = 'ingest_source'"),
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
    # A task put back until a host's slot opens is not claimed before then.
    not_before: Mapped[datetime.datetime | None]
    # WHEN PROGRESS LAST CHANGED, which is not when the row was last written.
    # A heartbeat proves the process is alive; this is the only column that can
    # say the WORK advanced, and telling those apart is the open problem a
    # wedged handler exposed. Set only on an actual change, so a handler
    # re-reporting the same numbers does not look like movement.
    progress_at: Mapped[datetime.datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    started_at: Mapped[datetime.datetime | None]
    finished_at: Mapped[datetime.datetime | None]


class TaskModelOverride(Base):
    """The model a person chose for a task, append-only.

    Latest row per purpose wins, and a NULL model is how an override is
    cleared - deleting the row would erase the fact that one existed, which is
    what a monthly review is looking for when a regression turns up weeks
    after the switch that caused it.
    """

    __tablename__ = "task_model_overrides"
    __table_args__ = (Index("idx_task_model_overrides_purpose", "purpose", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    # Recorded at decision time, not re-derived: the sanctioned set lives in
    # code and moves, so a row holding only the model could not say later
    # whether it was an override when it was made.
    overrode_sanctioned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reason: Mapped[str | None] = mapped_column(Text)
    # True when the change was large enough to need acknowledging and was
    # acknowledged. "He was told and went ahead" is a different fact from "he
    # changed it", and the review is where that distinction is wanted.
    acknowledged_cost: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    changed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class WorkerStatus(Base):
    __tablename__ = "worker_status"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    current_task_id: Mapped[int | None] = mapped_column(BigInteger)
    # What this worker will claim. Reported by the worker rather than inferred,
    # because the filters live in host environment and nothing else can see
    # them. queue_stalled reads these so a host is not called stalled by work
    # it is configured to refuse.
    kinds: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    excluded_kinds: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # The address this worker speaks from; two containers on one box share it.
    egress_group: Mapped[str | None] = mapped_column(Text)
    # The image commit this worker runs (JOBTRACKER_REVISION), so a host that
    # missed a roll is a query, not a sweep of seven containers. gcp-vps sat
    # two rolls behind for an hour on 2026-09-04 and nothing said so.
    release: Mapped[str | None] = mapped_column(Text)


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
