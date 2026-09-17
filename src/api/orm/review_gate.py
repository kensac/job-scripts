from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class ReviewGateDecision(Base):
    __tablename__ = "review_gate_decisions"
    __table_args__ = (
        UniqueConstraint("task_id", "url", name="uq_review_gate_decisions_task_url"),
        CheckConstraint("action IN ('skip','review')", name="ck_review_gate_decisions_action"),
        Index("idx_review_gate_decisions_url_created", "url", "created_at"),
        Index("idx_review_gate_decisions_created", "created_at"),
        Index("idx_review_gate_decisions_user_created", "user_id", "created_at"),
    )

    # Identity references intentionally outlive task, catalog and filter retention.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger)
    url: Mapped[str] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    filter_id: Mapped[int | None] = mapped_column(BigInteger)
    managed_board_id: Mapped[int | None] = mapped_column(BigInteger)
    revision: Mapped[int | None] = mapped_column(BigInteger)
    prompt_hash: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    profile_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    policy: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)
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
