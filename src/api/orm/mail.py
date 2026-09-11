"""Messages, what they were read to mean, and the applications they attach to."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("user_id", "provider_message_id", name="uq_email_messages_provider_id"),
        Index("idx_email_messages_thread", "user_id", "provider_thread_id"),
        Index("idx_email_messages_sent", "user_id", "sent_at"),
        Index("idx_email_messages_unclassified", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    provider_message_id: Mapped[str] = mapped_column(Text)
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    # Outlook's ThreadTopic: a normalised subject, not a conversation id. Kept
    # because it groups mail usefully within one employer, and named for what
    # it is so nothing reads it as identity again.
    thread_topic: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    from_email: Mapped[str | None] = mapped_column(Text)
    from_name: Mapped[str | None] = mapped_column(Text)
    to_emails: Mapped[Any] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    subject: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    body_text: Mapped[str | None] = mapped_column(Text)
    # The markup the message actually arrived as, kept so a reader can render
    # mail as mail. body_text stays the derived plain text and remains what
    # the classifier reads and what mention offsets index into - two fields
    # with two jobs, deliberately not one.
    #
    # NULL means the markup was not recoverable, not that the mail was plain:
    # imports before this column existed discarded it, and the import path
    # streams the archive without retaining a copy.
    body_html: Mapped[str | None] = mapped_column(Text)
    headers: Mapped[Any | None] = mapped_column(JSONB)
    prefilter_hit: Mapped[bool | None] = mapped_column(Boolean)
    prefilter_reason: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )


class EmailEvent(Base):
    __tablename__ = "email_events"
    __table_args__ = (Index("idx_email_events_latest", "message_id", "kind", text("id DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("email_messages.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deadline_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deadline_inferred: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    detail: Mapped[Any | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(Text)
    # `model` says which machine wrote this, `actor_user_id` says which human.
    # Both NULL has never happened and would be a bug.
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        Index("idx_applications_user_job", "user_id", "job_id"),
        Index("idx_applications_company", "user_id", text("lower(company_name)")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    # Nullable on purpose: an application predating the catalog has no posting
    # and never will.
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL")
    )
    company_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    source_provenance: Mapped[str] = mapped_column(Text, server_default="email")
    applied_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # A dismissal is a correction, not a delete: the application stays, stops
    # counting, and can be restored. Only mail-derived applications can be
    # dismissed - a tracker application exists because the user entered it.
    dismissed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dismissed_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )


class ApplicationMatch(Base):
    __tablename__ = "application_matches"
    __table_args__ = (
        Index("idx_application_matches_latest", "message_id", text("id DESC")),
        Index("idx_application_matches_app", "application_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("email_messages.id", ondelete="CASCADE")
    )
    # NULL records "we looked and found nothing", which is a different fact
    # from never having looked.
    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE")
    )
    method: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    # Which HUMAN wrote this row; NULL means the matcher did. Whether that
    # human was the owner or an administrator is derived by comparing this
    # against the message's owner rather than stored a second time.
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )


class ActionItem(Base):
    __tablename__ = "action_items"
    __table_args__ = (
        Index(
            "idx_action_items_open",
            "user_id",
            "due_at",
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE")
    )
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("email_events.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_by_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("email_events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_now
    )


class UserOAuthToken(Base):
    """A user's stored OAuth grant for one external provider.

    One row per (user, provider), not an append-only log: unlike a verdict, a
    superseded refresh token has no historical value and is a live security
    object, so reconnecting replaces rather than appends.

    Nothing here records a "connected"/"needs reconnect" status. The only fact
    worth storing is the observation that the provider rejected the refresh
    token, which is `invalid_at`; the state the UI renders is derived from it.
    """

    __tablename__ = "user_oauth_tokens"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    refresh_token_enc: Mapped[bytes] = mapped_column(BYTEA)
    access_token_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    access_token_expires_at: Mapped[datetime.datetime | None]
    # What the provider actually GRANTED, which is not necessarily what we
    # asked for: a user can decline an individual scope on the consent screen
    # and Google still returns a token.
    scopes: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    account_email: Mapped[str | None] = mapped_column(Text)
    invalid_at: Mapped[datetime.datetime | None]
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
