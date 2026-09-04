from __future__ import annotations

import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.embeddings import EMBEDDING_DIMENSIONS

_now = text("now()")


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy's declarative API
        datetime.datetime: TIMESTAMP(timezone=True),
        list[str]: ARRAY(Text),
        dict: JSONB,
    }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sub: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    groups: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class GroupBudget(Base):
    __tablename__ = "group_budgets"

    group_name: Mapped[str] = mapped_column(Text, primary_key=True)
    weekly_token_budget: Mapped[int | None] = mapped_column(BigInteger)
    # NULL = default policy (unlimited tiers: every server-keyed model;
    # budgeted tiers: the env allowlist). A list = exactly these models.
    allowed_models: Mapped[list[str] | None] = mapped_column(ARRAY(Text))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_source", "source"),
        Index("idx_jobs_uploaded_by", "uploaded_by"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    raw_url: Mapped[str] = mapped_column(Text, server_default=text("''"))
    company: Mapped[str] = mapped_column(Text, server_default=text("''"))
    title: Mapped[str] = mapped_column(Text, server_default=text("''"))
    locations: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    terms: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    source: Mapped[str] = mapped_column(Text)
    # NOT "this role is open". This is feed state: catalog.upsert_postings
    # writes it straight from whatever the board last said (active =
    # EXCLUDED.active) and NOTHING in this codebase ever clears it. So a board
    # that only ever lists live postings keeps every row it has supplied at
    # true forever, while a feed carrying an explicit per-posting flag
    # accumulates false. That is a difference in feed format, not in the job.
    #
    # It is therefore not comparable across sources, and reading it as closure
    # has already shipped one user-facing bug: 478 applications were badged
    # "no longer live" off this flag, of which 114 had a closed-check saying
    # the posting was OPEN and 363 had never been checked at all. Exactly one
    # was backed by evidence.
    #
    # For "is this role still open", use the closed check - an AI verdict
    # against the posting url, applied uniformly across boards. Job rows serve
    # it as `closed_verdict` ('open' | 'closed' | NULL for never checked).
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    date_posted: Mapped[datetime.datetime | None]
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    extraction_status: Mapped[str | None] = mapped_column(Text)
    comp_min: Mapped[int | None] = mapped_column(BigInteger)
    comp_max: Mapped[int | None] = mapped_column(BigInteger)
    comp_text: Mapped[str | None] = mapped_column(Text)
    comp_period: Mapped[str | None] = mapped_column(Text)
    comp_currency: Mapped[str | None] = mapped_column(Text)
    comp_basis: Mapped[str | None] = mapped_column(Text)
    comp_extracted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class JobRequirements(Base):
    """What a posting says it requires, keyed by url rather than by job id.

    No foreign key to jobs, deliberately: a quarter of the urls with stored page
    text have no job row, and those postings are closed and unscrapable. Same
    reasoning as ai_queries - a cache of paid AI work outlives the job row.
    """

    __tablename__ = "job_requirements"
    __table_args__ = (
        Index("idx_job_requirements_seniority", "seniority"),
        Index("idx_job_requirements_employment", "employment_type"),
    )

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    has_requirements: Mapped[bool] = mapped_column(Boolean)
    # NULL is "the posting does not say", which is not "zero" and not "none".
    yoe_min: Mapped[int | None]
    yoe_max: Mapped[int | None]
    degree_min: Mapped[str | None] = mapped_column(Text)
    degree_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    degree_fields: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    enrollment_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    seniority: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(Text)
    clearance: Mapped[str | None] = mapped_column(Text)
    citizenship_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sponsorship: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    # The ai_queries row the answer was read from. Ids are not TOASTed, so
    # "is there a newer page for this url" is an index read rather than a
    # detoast of the corpus; the hash above then decides whether the text
    # actually changed and the work needs paying for again.
    content_row_id: Mapped[int | None] = mapped_column(BigInteger)
    extracted_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class JobEmbedding(Base):
    """One vector per posting, for "what else reads like this".

    Separate from JobRequirements rather than a column on it: the two sweeps
    fail independently, and the requirements slice does SELECT DISTINCT r.*,
    which would drag a 6 KB vector through a DISTINCT on every request.

    No vector index, deliberately - see the migration for the measurements.
    The query that gets issued is always scoped to one user's visible slice,
    where an exact scan is single-digit milliseconds.
    """

    __tablename__ = "job_embeddings"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    model: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    # The ai_queries row the answer was read from. Ids are not TOASTed, so
    # "is there a newer page for this url" is an index read rather than a
    # detoast of the corpus; the hash above then decides whether the text
    # actually changed and the work needs paying for again.
    content_row_id: Mapped[int | None] = mapped_column(BigInteger)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # NULL means the model had no published price, which must stay distinct
    # from a call that genuinely cost nothing. Ten decimal places rather than
    # the six elsewhere: one embedding costs $0.0000226, which six places
    # rounds up by 1.6% every time - see the migration.
    cost_usd: Mapped[Any | None] = mapped_column(Numeric(14, 10))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class JobSkill(Base):
    """One row per skill a posting names, canonical form beside the raw text.

    Rows rather than an array column because the whole feature is a GROUP BY
    over a filtered slice. skill_raw is the primary key component so two raw
    spellings may collapse onto one canonical skill without colliding, and so a
    better normalisation is an UPDATE rather than another paid AI pass.
    """

    __tablename__ = "job_skills"
    __table_args__ = (Index("idx_job_skills_skill", "skill", "kind"),)

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_raw: Mapped[str] = mapped_column(Text, primary_key=True)
    skill: Mapped[str] = mapped_column(Text)


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


class Source(Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    listings_url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class SourceGroup(Base):
    __tablename__ = "source_groups"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    members: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserSource(Base):
    __tablename__ = "user_sources"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(Text, primary_key=True)


class UserFilter(Base):
    __tablename__ = "user_filters"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    on_ambiguous: Mapped[str] = mapped_column(Text, server_default=text("'keep'"))
    fail_closed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    prompt_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    column_layout: Mapped[Any | None] = mapped_column(JSONB)
    prefs: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    api_key_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    bypass_sponsorship_filter: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    criteria: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    ai_provider: Mapped[str] = mapped_column(Text, server_default=text("'openai'"))
    ai_base_url: Mapped[str | None] = mapped_column(Text)
    ai_model: Mapped[str | None] = mapped_column(Text)
    ai_params: Mapped[dict] = mapped_column(server_default=text("'{}'::jsonb"))
    email_digest: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    digest_token: Mapped[str | None] = mapped_column(Text, unique=True)
    last_digest_at: Mapped[datetime.datetime | None]
    # The addresses this mailbox's owner says are theirs. NOT a confirmation
    # flag beside a derived set: this IS the confirmation, and "has the user
    # confirmed" is `identities_confirmed_at IS NOT NULL`, derived from the
    # same row rather than tracked separately where the two could disagree.
    #
    # It may differ from what core.identity derives in BOTH directions - a
    # derived address the user drops, and an old address of theirs with no mail
    # left in the corpus for us to have found. So it is stored rather than
    # diffed against the derivation.
    identities: Mapped[list | None] = mapped_column(JSONB)
    identities_confirmed_at: Mapped[datetime.datetime | None]
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    __table_args__ = (
        Index("idx_api_usage_user_created", "user_id", "created_at"),
        Index("idx_api_usage_purpose", "purpose", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # NULL for fleet work. Catalog-wide extraction is charged to nobody in
    # particular, and attributing it to whichever admin is user 1 would make
    # per-user spend a fiction.
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
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
    # NULL means the model had no published price, which must stay distinct
    # from a call that genuinely cost nothing.
    cost_usd: Mapped[Any | None] = mapped_column(Numeric(12, 6))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_status", "status", "id"),
        Index(
            "idx_tasks_parent",
            "parent_id",
            "status",
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    parent_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    worker: Mapped[str | None] = mapped_column(Text)
    last_heartbeat: Mapped[datetime.datetime | None]
    progress: Mapped[Any | None] = mapped_column(JSONB)
    # WHEN PROGRESS LAST CHANGED, which is not when the row was last written.
    # A heartbeat proves the process is alive; this is the only column that can
    # say the WORK advanced, and telling those apart is the open problem a
    # wedged handler exposed. Set only on an actual change, so a handler
    # re-reporting the same numbers does not look like movement.
    progress_at: Mapped[datetime.datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    started_at: Mapped[datetime.datetime | None]
    finished_at: Mapped[datetime.datetime | None]


class TaskModelOverride(Base):
    """The model a person chose for a task, append-only.

    Latest row per purpose wins, and a NULL model is how an override is
    cleared - deleting the row would erase the fact that one existed, which is
    what a monthly review is looking for when a regression turns up weeks
    after the switch that caused it.
    """

    __tablename__ = "task_model_overrides"
    __table_args__ = (Index("idx_task_model_overrides_purpose", "purpose", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    purpose: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    # Recorded at decision time, not re-derived: the sanctioned set lives in
    # code and moves, so a row holding only the model could not say later
    # whether it was an override when it was made.
    overrode_sanctioned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reason: Mapped[str | None] = mapped_column(Text)
    # True when the change was large enough to need acknowledging and was
    # acknowledged. "He was told and went ahead" is a different fact from "he
    # changed it", and the review is where that distinction is wanted.
    acknowledged_cost: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    changed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class AiPrompt(Base):
    """One row per distinct instruction text, whatever sends it.

    Production carries 21 distinct prompts across 68,735 ai_queries rows, so
    the text is affordable here in a way it is not per-row: 32 KB against the
    75 MB the same text costs stored beside every request that used it.

    Not a resolution key. Changing a filter's prompt is meant to fork its
    verdict log; changing an extraction prompt must not invalidate the catalog.
    """

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
    """A bounded sample of what a prompt version actually produced.

    Bounded because a sample is what answers "what changed", and the
    destination tables already hold the current answer - what they do not hold
    is the previous one, which is the half a prompt-change review needs.
    """

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
    prompt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("ai_prompts.id"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    est_cost_usd: Mapped[Any | None] = mapped_column(Numeric(12, 6))
    submitted_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    completed_at: Mapped[datetime.datetime | None]


class WorkerStatus(Base):
    __tablename__ = "worker_status"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    current_task_id: Mapped[int | None] = mapped_column(BigInteger)


class HealthAlert(Base):
    __tablename__ = "health_alerts"
    __table_args__ = (
        Index(
            "uq_health_alerts_open",
            "kind",
            "subject",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, server_default=text("'warning'"))
    message: Mapped[str] = mapped_column(Text, server_default=text("''"))
    detail: Mapped[Any | None] = mapped_column(JSONB)
    first_seen: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    notified_at: Mapped[datetime.datetime | None]
    resolved_at: Mapped[datetime.datetime | None]


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


class SourceRequest(Base):
    __tablename__ = "source_requests"
    __table_args__ = (Index("idx_source_requests_status", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    resolved_at: Mapped[datetime.datetime | None]


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("idx_reports_status", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, server_default=text("''"))
    corrections: Mapped[Any | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    resolved_at: Mapped[datetime.datetime | None]


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
