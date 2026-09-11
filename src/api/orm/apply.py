"""The assisted apply: the profile it fills from, what it filled, and what it was told."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
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
    page: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ExtensionRecipe(Base):
    """One publish of a config-driven reader's table (api.apply.recipes):
    the newest enabled row per adapter is what the extension fetches; the
    copy bundled in the extension is its fallback."""

    __tablename__ = "extension_recipes"
    __table_args__ = (
        UniqueConstraint("adapter", "revision", name="uq_extension_recipes_adapter_revision"),
        Index("idx_extension_recipes_adapter_enabled", "adapter", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    adapter: Mapped[str] = mapped_column(Text)
    revision: Mapped[str] = mapped_column(Text)
    body: Mapped[Any] = mapped_column(JSONB)
    published_by: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class SuggestionResponse(Base):
    """What the user decided about a suggestion, which is the only fact here.

    The suggestions themselves are derived at read time - a comparison of what
    the mail says against what the board says - so they correct themselves when
    either side changes. Storing them would freeze a disagreement that should
    disappear on its own.
    """

    __tablename__ = "suggestion_responses"
    __table_args__ = (Index("idx_suggestion_responses_app", "application_id", "event_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE")
    )
    # Keyed on the evidence, so a dismissal silences THIS event rather than the
    # question. A later rejection from the same company gets asked again.
    event_id: Mapped[int | None] = mapped_column(BigInteger)
    suggested_status: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )
