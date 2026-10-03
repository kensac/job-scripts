from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class ReviewGatePolicySnapshot(Base):
    __tablename__ = "review_gate_policies"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    policy: Mapped[dict] = mapped_column(JSONB)


class ReviewGateUrl(Base):
    """One row per posting URL a decision was ever recorded for.

    The per-task row holds this id instead of the URL text, so the unique
    (task_id, url_id) and the url_id lookup index are a few bytes an entry
    where the URL-keyed ones averaged 184 B (uq_review_gate_decisions_task_url,
    1.4 GB over 7.6M rows, 2026-10-03). The URL is unique here, so
    (task_id, url_id) is unique exactly when (task_id, url) is.
    """

    __tablename__ = "review_gate_urls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)


class ReviewGateDecisionBody(Base):
    """A decision's immutable content, stored once however many tasks repeat it.

    On 2026-10-02 production recorded 570,747 decisions holding 38,017
    distinct bodies: every task re-admits the same postings under the same
    prompt and policy. The digest is SHA-256 of the JSONB text of the content
    array (review_decision_storage.BODY_DIGEST); a digest match is accepted
    only after exact comparison of every column.
    """

    __tablename__ = "review_gate_decision_bodies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_id"],
            ["review_gate_policies.id"],
            name="fk_review_gate_decision_bodies_policy",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action IN ('skip','review')", name="ck_review_gate_decision_bodies_action"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    prompt_hash: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    profile_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    policy_id: Mapped[int] = mapped_column(BigInteger)
    evidence: Mapped[dict] = mapped_column(JSONB)


class ReviewGateDecision(Base):
    __tablename__ = "review_gate_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_id"],
            ["review_gate_policies.id"],
            name="fk_review_gate_decisions_policy",
            ondelete="RESTRICT",
            postgresql_not_valid=True,
        ),
        ForeignKeyConstraint(
            ["url_id"],
            ["review_gate_urls.id"],
            name="fk_review_gate_decisions_url",
            ondelete="RESTRICT",
            postgresql_not_valid=True,
        ),
        ForeignKeyConstraint(
            ["body_id"],
            ["review_gate_decision_bodies.id"],
            name="fk_review_gate_decisions_body",
            ondelete="RESTRICT",
            postgresql_not_valid=True,
        ),
        # Every row keeps one complete shape: the URL inline or by reference,
        # and the body inline or by reference. These are the NOT NULLs the
        # inline columns had before they could move.
        CheckConstraint(
            "(url IS NOT NULL OR url_id IS NOT NULL) AND (body_id IS NOT NULL OR "
            "(prompt_hash IS NOT NULL AND stage IS NOT NULL AND mode IS NOT NULL "
            "AND action IS NOT NULL AND title IS NOT NULL AND evidence IS NOT NULL))",
            name="ck_review_gate_decisions_shape",
            postgresql_not_valid=True,
        ),
        UniqueConstraint("task_id", "url", name="uq_review_gate_decisions_task_url"),
        Index("uq_review_gate_decisions_task_url_id", "task_id", "url_id", unique=True),
        # The same job drawer read for reference-only rows (URL_MATCH).
        Index("idx_review_gate_decisions_url_id", "url_id"),
        CheckConstraint("action IN ('skip','review')", name="ck_review_gate_decisions_action"),
        # Zero scans in production as of 2026-10-03, kept anyway: a person's
        # job drawer reads decisions by url, and without this the plan walks
        # the whole (task_id, url) unique index. Measured in 7c0b33a7fd95.
        Index("idx_review_gate_decisions_url_created", "url", "created_at"),
        Index("idx_review_gate_decisions_created", "created_at"),
    )

    # Identity references intentionally outlive task, catalog and filter retention.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger)
    # Inline columns are the legacy shape; url_id and body_id replace them.
    # Readers resolve both through review_decision_storage.DECISIONS.
    url: Mapped[str | None] = mapped_column(Text)
    url_id: Mapped[int | None] = mapped_column(BigInteger)
    body_id: Mapped[int | None] = mapped_column(BigInteger)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    filter_id: Mapped[int | None] = mapped_column(BigInteger)
    managed_board_id: Mapped[int | None] = mapped_column(BigInteger)
    revision: Mapped[int | None] = mapped_column(BigInteger)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    profile_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    policy_id: Mapped[int | None] = mapped_column(BigInteger)
    policy: Mapped[dict | None] = mapped_column(JSONB)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ReviewGateOutcome(Base):
    __tablename__ = "review_gate_outcomes"
    __table_args__ = (
        UniqueConstraint("decision_id", "query_id", name="uq_review_gate_outcomes_decision_query"),
        Index("idx_review_gate_outcomes_decision", "decision_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    decision_id: Mapped[int] = mapped_column(BigInteger)
    query_id: Mapped[int] = mapped_column(BigInteger)
    batch_id: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    rejected: Mapped[bool | None] = mapped_column(Boolean)
    outcome: Mapped[str] = mapped_column(Text)
    recorded_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric)
    usage: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
