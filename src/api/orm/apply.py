"""The assisted apply: the profile it fills from, what it filled, and what it was told."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    UUID,
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class UserResume(Base):
    """A resume a person keeps here, as text: pasted, or read out of a PDF
    at upload. The drafts in application_answers are written from it."""

    __tablename__ = "user_resumes"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    # The file itself, kept only so the extension can attach it to a form;
    # NULL for a resume that was pasted.
    pdf: Mapped[bytes | None] = mapped_column(BYTEA)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApplicationForm(Base):
    """The questions a posting's application form asks, read once per url
    from the ATS (core.fetching.forms). questions is NULL when the host cannot be
    read; error says why the last read failed."""

    __tablename__ = "application_forms"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    questions: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApplicationAnswer(Base):
    """One person's draft for one question on one job's form. source is
    'form' for a question read off the ATS and 'manual' for one pasted in;
    turns is the back-and-forth that produced the draft."""

    __tablename__ = "application_answers"
    __table_args__ = (UniqueConstraint("user_id", "job_id", "key"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default=text("'form'"))
    required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    draft_revision: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    draft: Mapped[str | None] = mapped_column(Text)
    turns: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    model: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApplicationAnswerBank(Base):
    """What one person typed into a form field the profile could not fill,
    keyed by the field's normalised label so the next form asking the same
    thing is filled from it (api.apply)."""

    __tablename__ = "application_answer_bank"
    __table_args__ = (UniqueConstraint("user_id", "label_norm"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(Text)
    label_norm: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    times_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_used_at: Mapped[datetime.datetime | None]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApplicationFill(Base):
    """One form the extension filled: every field, which rung filled it,
    and what the person changed before submitting. The ledger the fill
    rate and the most-often-blank labels are read from, and the corpus a
    resolver change is measured against."""

    __tablename__ = "application_fills"
    __table_args__ = (Index("application_fills_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    fields: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    submitted_at: Mapped[datetime.datetime | None]
    # The application this submit recorded. Set at submit, with the job's
    # application or, for a form whose posting is not on the board, one of
    # provenance `apply`.
    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="SET NULL")
    )


class ApplicationReport(Base):
    """A page the extension could not handle, as it saw it: the fields it
    read, what the API resolved, the form's markup, and the person's note.
    Read when triaging what to teach the reader or the resolver next."""

    __tablename__ = "application_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApplicationReportUpload(Base):
    """A report upload stays addressable after its chunks become a report."""

    __tablename__ = "application_report_uploads"
    __table_args__ = (UniqueConstraint("report_id"),)

    id: Mapped[Any] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    field_count: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(BigInteger)
    report_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("application_reports.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApplicationReportChunk(Base):
    """One bounded part of a report until the whole report is committed."""

    __tablename__ = "application_report_chunks"

    upload_id: Mapped[Any] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("application_report_uploads.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chunk_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    body: Mapped[bytes] = mapped_column(BYTEA)


class ExtensionRecipe(Base):
    """One publish of a config-driven reader's table (api.apply.recipes):
    the newest enabled row per adapter is what the extension fetches; the
    copy bundled in the extension is its fallback."""

    __tablename__ = "extension_recipes"
    __table_args__ = (
        UniqueConstraint("adapter", "revision", name="uq_extension_recipes_adapter_revision"),
        UniqueConstraint("upload_id", name="uq_extension_recipes_upload_id"),
        Index("idx_extension_recipes_adapter_enabled", "adapter", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    adapter: Mapped[str] = mapped_column(Text)
    revision: Mapped[str] = mapped_column(Text)
    body: Mapped[Any | None] = mapped_column(JSONB)
    upload_id: Mapped[Any | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extension_recipe_uploads.id", name="fk_extension_recipes_upload_id"),
    )
    published_by: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class ExtensionRecipeUpload(Base):
    """An immutable upload and the state of its validation."""

    __tablename__ = "extension_recipe_uploads"

    id: Mapped[Any] = mapped_column(UUID(as_uuid=True), primary_key=True)
    adapter: Mapped[str] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(BigInteger)
    published_by: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    revision: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    next_chunk: Mapped[int] = mapped_column(BigInteger, server_default="0")
    validation_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ExtensionRecipeChunk(Base):
    """One bounded part of a recipe upload."""

    __tablename__ = "extension_recipe_chunks"

    upload_id: Mapped[Any] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extension_recipe_uploads.id"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    body: Mapped[bytes] = mapped_column(BYTEA)


class ExtensionRecipeFrame(Base):
    """One persisted JSON container in an upload validator."""

    __tablename__ = "extension_recipe_frames"

    upload_id: Mapped[Any] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extension_recipe_uploads.id"), primary_key=True
    )
    depth: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
