"""What was asked of a model, what it answered, and what that cost."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class AiQuery(Base):
    """Every AI verdict and every stored page text, one row per call.

    The oldest table here: created by core/store.py's own DDL from the first
    commit and adopted by alembic in f3a4b5c6d7e8, so this class mirrors what
    production holds rather than what a fresh design would choose. Keyed by
    url, not job id, because a cache of paid AI work outlives the job row.

    The two INCLUDE indexes serve the board's "latest verdict per url" reads
    as index-only scans: the DISTINCT ON over closed/clearance rows went from
    a 1.2 s seq scan and sort to 50 ms on a copy of production. They only do
    that while the visibility map is warm, which the per-table autovacuum
    settings in the migration keep true; the table takes 44% updates, and
    the stock thresholds let 20% of it go dead before acting.
    """

    __tablename__ = "ai_queries"
    __table_args__ = (
        Index("idx_ai_queries_batch", "batch_id"),
        Index("idx_ai_queries_url", "url"),
        Index("idx_ai_queries_url_check", "url", "check_type"),
        Index("idx_ai_queries_status", "status"),
        Index("idx_ai_queries_check_type", "check_type"),
        Index("idx_ai_queries_created_at", "created_at"),
        # The admin filter options skip-scan the distinct values of these two
        # and keep those seen in the last 30 days (routers/admin/queries.py).
        Index("idx_ai_queries_config_recent", "config_name", "created_at"),
        Index("idx_ai_queries_worker_recent", "worker", "created_at"),
        Index("idx_ai_queries_prompt_hash", "check_type", "prompt_hash"),
        Index(
            "idx_ai_queries_latest_verdict",
            "url",
            "check_type",
            text("id DESC"),
            # created_at is for the company page's last_checked_at: without
            # it every verdict the page reads is a heap fetch.
            postgresql_include=["status", "created_at"],
            postgresql_where=text(
                "check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')"
            ),
        ),
        Index(
            "idx_ai_queries_latest_custom",
            "url",
            "prompt_hash",
            text("id DESC"),
            postgresql_include=["status"],
            postgresql_where=text("check_type = 'custom' AND status IN ('passed', 'rejected')"),
        ),
        # url serves the per-host fetch pacing LIKE, reason the reason-group
        # regex. company and job_title had one each for the admin search;
        # production never scanned either, so that search is a seq scan.
        *(
            Index(
                f"idx_ai_queries_{col}_trgm",
                col,
                postgresql_using="gin",
                postgresql_ops={col: "gin_trgm_ops"},
            )
            for col in ("url", "reason")
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    config_name: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    check_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    reasoning_effort: Mapped[str | None] = mapped_column(Text)
    filter_name: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    job_title: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)
    instructions_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "ai_instruction_texts.id",
            name="fk_ai_queries_instructions_id",
            postgresql_not_valid=True,
        ),
    )
    input_content: Mapped[str | None] = mapped_column(Text)
    parsed_json: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cached_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    worker: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    # sha256 of the exact question a verdict answers: model, effort, schema,
    # instructions and input text. NULL is unknown provenance, never a match.
    # Re-verification reuses an answer whose question is unchanged instead of
    # buying it again (tasks/verify.py).
    request_sha256: Mapped[str | None] = mapped_column(Text)


class AiInstructionText(Base):
    __tablename__ = "ai_instruction_texts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sha256: Mapped[str] = mapped_column(Text, unique=True)
    instructions: Mapped[str] = mapped_column(Text)


class AiBatch(Base):
    __tablename__ = "ai_batches"
    __table_args__ = (
        Index("idx_ai_batches_status", "status", "id"),
        Index("idx_ai_batches_task", "task_id"),
        CheckConstraint(
            "payer IN ('fleet', 'user', 'managed_board') "
            "AND (payer = 'fleet') = (payer_id IS NULL)",
            name="ck_ai_batches_payer",
        ),
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
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    est_cost_usd: Mapped[Any | None] = mapped_column(Numeric(12, 6))
    submitted_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    completed_at: Mapped[datetime.datetime | None]
    # Who pays for the batch's requests, recorded by whoever submits it.
    # NULL is a batch from before this was recorded, which is not the fleet:
    # its calls are left to the ledger backfill rather than guessed.
    payer: Mapped[str | None] = mapped_column(Text)
    payer_id: Mapped[int | None] = mapped_column(BigInteger)


class BatchRequest(Base):
    __tablename__ = "batch_requests"

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    custom_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # A version 3 bundle member reference, the only stored shape
    # (observability.md, request snapshot storage).
    snapshot_ref: Mapped[dict] = mapped_column(JSONB)


class BatchResultReceipt(Base):
    __tablename__ = "batch_result_receipts"
    __table_args__ = (Index("idx_batch_result_receipts_task", "task_id", "consumed_at"),)

    provider_batch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    custom_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"))
    response: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    consumed_at: Mapped[datetime.datetime | None]


class AiBatchError(Base):
    """Every per-request error a provider batch returned, as the provider
    wrote it. The batch row says how many failed; this says why, which is
    the only thing that distinguishes a rejected submission from a model
    that cannot take the schema. Stored whole and groomed later."""

    __tablename__ = "ai_batch_errors"
    __table_args__ = (Index("idx_ai_batch_errors_batch", "provider_batch_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider_batch_id: Mapped[str] = mapped_column(Text)
    custom_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    __table_args__ = (
        Index("idx_api_usage_user_created", "user_id", "created_at"),
        Index("idx_api_usage_purpose", "purpose", "created_at"),
        Index("idx_api_usage_managed_board_created", "managed_board_id", "created_at"),
        CheckConstraint(
            "user_id IS NULL OR managed_board_id IS NULL", name="ck_api_usage_single_subject"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # NULL for fleet work. Catalog-wide extraction is charged to nobody in
    # particular, and attributing it to whichever admin is user 1 would make
    # per-user spend a fiction.
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    managed_board_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("managed_boards.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    key_source: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    batched: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    cached_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    # NULL means the model had no published price, which must stay distinct
    # from a call that genuinely cost nothing.
    cost_usd: Mapped[Any | None] = mapped_column(Numeric(12, 6))


class ModelCall(Base):
    """One paid provider request, written only by api.model_calls.

    A batch item is identified by (provider_batch_id, custom_id), so a
    replayed receipt adds nothing; a live call has no provider identity.
    docs/agents/architecture-migration.md ("The ledger of paid model calls")
    is the design and the measurements behind it.
    """

    __tablename__ = "model_calls"
    __table_args__ = (
        UniqueConstraint("provider_batch_id", "custom_id", name="uq_model_calls_batch_item"),
        # A batch that left no per-request record is one row for the whole
        # batch, and only one.
        Index(
            "uq_model_calls_batch_aggregate",
            "provider_batch_id",
            unique=True,
            postgresql_where=text("custom_id IS NULL AND provider_batch_id IS NOT NULL"),
        ),
        # A backfilled row names the row it came from, so a resumed backfill
        # cannot copy it twice.
        Index(
            "uq_model_calls_source_row",
            "source",
            "source_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
        Index("idx_model_calls_created", "created_at"),
        Index("idx_model_calls_user_created", "user_id", "created_at"),
        Index("idx_model_calls_managed_board_created", "managed_board_id", "created_at"),
        CheckConstraint(
            "user_id IS NULL OR managed_board_id IS NULL", name="ck_model_calls_single_payer"
        ),
        CheckConstraint(
            "custom_id IS NULL OR provider_batch_id IS NOT NULL", name="ck_model_calls_batch_item"
        ),
        CheckConstraint(
            "provider_batch_id IS NULL OR batched", name="ck_model_calls_batch_is_batched"
        ),
        CheckConstraint(
            "requests = 1 OR (custom_id IS NULL AND provider_batch_id IS NOT NULL)",
            name="ck_model_calls_requests",
        ),
        CheckConstraint(
            "(payer IS NULL OR payer IN ('fleet', 'user', 'managed_board')) "
            "AND (user_id IS NULL OR payer = 'user') "
            "AND (managed_board_id IS NULL OR payer = 'managed_board')",
            name="ck_model_calls_payer",
        ),
        CheckConstraint(
            "source IN ('call', 'verdict', 'receipt', 'batch', 'usage')",
            name="ck_model_calls_source",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    # Who pays: 'fleet', 'user' or 'managed_board'. NULL is a backfilled call
    # whose payer nothing recorded, which is not the fleet. The delete rules
    # are api_usage's, which this replaces, so a person's or a board's removal
    # behaves as it does today.
    payer: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    managed_board_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("managed_boards.id", ondelete="RESTRICT")
    )
    # NULL: a backfilled call from a record that did not say whose key.
    key_source: Mapped[str | None] = mapped_column(Text)
    batched: Mapped[bool] = mapped_column(Boolean)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    provider_batch_id: Mapped[str | None] = mapped_column(Text)
    custom_id: Mapped[str | None] = mapped_column(Text)
    # More than one only on a row standing for a whole batch (or the part of
    # one) that left no per-request record.
    requests: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    prompt_tokens: Mapped[int] = mapped_column(BigInteger)
    completion_tokens: Mapped[int] = mapped_column(BigInteger)
    total_tokens: Mapped[int] = mapped_column(BigInteger)
    cached_tokens: Mapped[int] = mapped_column(BigInteger)
    # NULL: the provider did not say, which is not zero writes.
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    # NULL: the record it was backfilled from did not keep it.
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger)
    # NULL: the model has no published price, never a free call.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # 'call': written when the call was made. The others are the backfill and
    # name the table it came from: a verdict, a batch receipt, a batch's
    # totals, or an api_usage row; source_id is that row's id.
    source: Mapped[str] = mapped_column(Text, server_default=text("'call'"))
    source_id: Mapped[int | None] = mapped_column(BigInteger)
