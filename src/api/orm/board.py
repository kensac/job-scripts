"""What a person keeps and what they are shown."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class BoardVisible(Base):
    """The computed membership of a person's board: every job the full
    visibility predicate admits, written by the recompute_board task and read
    by every board request. See api.board.visibility."""

    __tablename__ = "board_visible"
    __table_args__ = (Index("idx_board_visible_user", "user_id", "computed_at"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    computed_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserJobWorkingSet(Base):
    """Automated discovery scope, rebuildable independently of person state."""

    __tablename__ = "user_job_working_set"
    __table_args__ = (Index("idx_user_job_working_set_job", "job_id"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )


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
    person_touched_at: Mapped[datetime.datetime | None]
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


class SavedView(Base):
    """A named state of one list page, per user: its filters, its sort order
    across columns, its columns, its search. Clicking a view applies all of
    it. `page` is the surface key the frontend uses (board, pipeline, admin
    queue, admin mail, ...); `state` is the page's canonical request shape,
    the same parameter names the API echoes back in `filters`, `sorts` and
    `sortable`, so a view is reproducible against any build that serves
    those. One default per page; position orders the switcher.
    """

    __tablename__ = "saved_views"
    __table_args__ = (
        UniqueConstraint("user_id", "page", "name", name="uq_saved_views_user_page_name"),
        Index("idx_saved_views_user_page", "user_id", "page", "position"),
        Index(
            "uq_saved_views_user_page_default",
            "user_id",
            "page",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    page: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    state: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserFilter(Base):
    __tablename__ = "user_filters"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        Index(
            "uq_user_filters_one_enabled", "user_id", unique=True, postgresql_where=text("enabled")
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    on_ambiguous: Mapped[str] = mapped_column(Text, server_default=text("'keep'"))
    fail_closed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    prompt_hash: Mapped[str] = mapped_column(Text)
    # The preset this was adopted from, or NULL for a filter written by hand.
    # Provenance, not identity: the name stays editable, and retiring the
    # preset leaves the filter alone.
    preset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("filter_presets.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


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
