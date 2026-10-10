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


class AiPrompt(Base):
    """Nothing reads or writes this. It stays modelled until the release that
    drops it, because images that still write it may be running."""

    __tablename__ = "ai_prompts"
    __table_args__ = (Index("idx_ai_prompts_purpose", "purpose", "last_seen_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    prompt_hash: Mapped[str] = mapped_column(Text, unique=True)
    purpose: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    # Moves every sweep, so a retired prompt shows as one with an old
    # last_seen_at rather than by being absent.
    last_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    batches: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class AiPromptSample(Base):
    """Nothing reads or writes this; dropped with ai_prompts."""

    __tablename__ = "ai_prompt_samples"
    __table_args__ = (Index("idx_ai_prompt_samples_prompt", "prompt_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    prompt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ai_prompts.id", ondelete="CASCADE")
    )
    # A url, a message id - whatever the caller keyed its specs by. Not a
    # foreign key: the sample outlives the row it describes, which is most of
    # its value once a posting is gone.
    custom_id: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    # Sampled alongside outputs: a prompt edit that starts producing
    # unparseable JSON is exactly the change worth seeing, and it leaves no
    # output behind.
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class AiBatch(Base):
    __tablename__ = "ai_batches"
    __table_args__ = (
        Index("idx_ai_batches_status", "status", "id"),
        Index("idx_ai_batches_task", "task_id"),
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
    # Nothing reads or writes this; dropped with ai_prompts.
    prompt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("ai_prompts.id"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    est_cost_usd: Mapped[Any | None] = mapped_column(Numeric(12, 6))
    submitted_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    completed_at: Mapped[datetime.datetime | None]


class BatchRequest(Base):
    __tablename__ = "batch_requests"

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    custom_id: Mapped[str] = mapped_column(Text, primary_key=True)
    snapshot: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_ref: Mapped[dict | None] = mapped_column(JSONB)


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


class AiExperiment(Base):
    """One AI step measured across models and efforts on a seeded sample,
    through the production path. See tasks.experiments."""

    __tablename__ = "ai_experiments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    purpose: Mapped[str] = mapped_column(Text)
    params: Mapped[Any] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    summary: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    finished_at: Mapped[datetime.datetime | None] = mapped_column()


class AiExperimentResult(Base):
    """One arm's answer for one posting in an experiment."""

    __tablename__ = "ai_experiment_results"
    __table_args__ = (UniqueConstraint("experiment_id", "arm", "url"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ai_experiments.id", ondelete="CASCADE")
    )
    arm: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    output: Mapped[Any | None] = mapped_column(JSONB)
    usage: Mapped[Any | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error: Mapped[str | None] = mapped_column(Text)


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
