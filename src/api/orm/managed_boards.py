"""Sponsor-owned public board definitions and their rebuildable projection."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class ManagedBoard(Base):
    __tablename__ = "managed_boards"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_managed_boards_slug"),
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_managed_boards_slug_lower_kebab"
        ),
        CheckConstraint(
            "on_ambiguous IN ('keep', 'filter')", name="ck_managed_boards_on_ambiguous"
        ),
        CheckConstraint("revision >= 1", name="ck_managed_boards_revision_positive"),
        CheckConstraint(
            "public_revision IS NULL OR public_revision >= 1",
            name="ck_managed_boards_public_revision_positive",
        ),
        CheckConstraint(
            "(published AND published_at IS NOT NULL AND unpublished_at IS NULL "
            "AND public_revision IS NOT NULL) OR (NOT published)",
            name="ck_managed_boards_public_state",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    sponsor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT")
    )
    prompt: Mapped[str] = mapped_column(Text)
    prompt_hash: Mapped[str] = mapped_column(Text)
    requested_model: Mapped[str] = mapped_column(Text)
    on_ambiguous: Mapped[str] = mapped_column(Text, server_default=text("'keep'"))
    fail_closed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    criteria: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    published: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    revision: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    public_revision: Mapped[int | None] = mapped_column(BigInteger)
    projection_updated_at: Mapped[datetime.datetime | None]
    published_at: Mapped[datetime.datetime | None]
    unpublished_at: Mapped[datetime.datetime | None]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ManagedBoardSource(Base):
    __tablename__ = "managed_board_sources"
    __table_args__ = (Index("idx_managed_board_sources_source", "source", "managed_board_id"),)

    managed_board_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("managed_boards.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("sources.name", ondelete="RESTRICT"), primary_key=True
    )


class ManagedBoardJob(Base):
    __tablename__ = "managed_board_jobs"

    managed_board_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("managed_boards.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    projected_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    sort_at: Mapped[datetime.datetime]
    projection_revision: Mapped[int] = mapped_column(BigInteger)
    resolved_model: Mapped[str | None] = mapped_column(Text)


Index(
    "idx_managed_board_jobs_newest",
    ManagedBoardJob.managed_board_id,
    ManagedBoardJob.sort_at.desc(),
    ManagedBoardJob.job_id.desc(),
)
