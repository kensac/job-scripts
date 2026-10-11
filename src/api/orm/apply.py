"""The assisted apply: the profile it fills from, what it filled, and what it was told."""

from __future__ import annotations

import datetime
import uuid
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
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class UserResume(Base):
    """A resume a person keeps here, as text: pasted, or read out of a PDF
    at upload. The drafts in application_answers are written from it."""

    __tablename__ = "user_resumes"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    pdf_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("resume_uploads.id", name="fk_user_resumes_pdf_upload")
    )
    text_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("resume_text_versions.id", name="fk_user_resumes_text_version"),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ResumeUpload(Base):
    __tablename__ = "resume_uploads"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    received_size: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    next_part: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'receiving'"))
    error: Mapped[str | None] = mapped_column(Text)
    completed_resume_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ResumeUploadPart(Base):
    __tablename__ = "resume_upload_parts"

    upload_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("resume_uploads.id"), primary_key=True
    )
    part_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer)
    verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class ResumeTextVersion(Base):
    __tablename__ = "resume_text_versions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    chars: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    part_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    ready: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ResumeTextPart(Base):
    __tablename__ = "resume_text_parts"

    version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("resume_text_versions.id"), primary_key=True
    )
    part_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class UserResumeRevision(Base):
    __tablename__ = "user_resume_revisions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    resume_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    pdf_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("resume_uploads.id")
    )
    text_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("resume_text_versions.id")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ResumeBackfillRun(Base):
    """Durable part and verification progress for one old resume value."""

    __tablename__ = "resume_backfill_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_kind: Mapped[str] = mapped_column(Text)
    source_id: Mapped[int] = mapped_column(BigInteger)
    field: Mapped[str] = mapped_column(Text)
    source_xmin: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(BigInteger)
    part_count: Mapped[int] = mapped_column(BigInteger)
    next_part: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    verify_part: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    source_digest: Mapped[str] = mapped_column(Text)
    copy_digest: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    attempted_at: Mapped[datetime.datetime | None] = mapped_column()
    failed_at: Mapped[datetime.datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ResumeLegacyClearRun(Base):
    """Verification progress before one copied resume value is cleared."""

    __tablename__ = "resume_legacy_clear_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_kind: Mapped[str] = mapped_column(Text)
    source_id: Mapped[int] = mapped_column(BigInteger)
    field: Mapped[str] = mapped_column(Text)
    source_xmin: Mapped[str] = mapped_column(Text)
    canonical_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True))
    total: Mapped[int] = mapped_column(BigInteger)
    part_count: Mapped[int] = mapped_column(BigInteger)
    next_part: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    verified_digest: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    attempted_at: Mapped[datetime.datetime | None] = mapped_column()
    failed_at: Mapped[datetime.datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


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
    upload_id: Mapped[Any] = mapped_column(
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
