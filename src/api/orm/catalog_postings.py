"""Catalog postings, personal uploads, and retained page observations."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_source", "source"),
        # The admin ledger's per-source job count and newest posting, read
        # from the index alone instead of the heap: 1,110 buffers against
        # 12,882 for a seq scan at 224,541 jobs (test copy, 2026-10-04).
        Index("idx_jobs_source_created", "source", "created_at"),
        # company_key, which the admin company page reads per page of names.
        # Off the busiest names it turns each per-page read from a seq scan
        # into a probe: the repost group 98 ms to 10 ms, the open share
        # 400 ms to 90 ms (synthetic catalog, 2026-10-04).
        Index("idx_jobs_company_key", text("lower(btrim(company))")),
        # verify_new's twin lookup (core.near_copy).
        Index(
            "idx_jobs_near_copy",
            "source",
            "near_copy_key",
            postgresql_where=text("near_copy_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    raw_url: Mapped[str] = mapped_column(Text, server_default=text("''"))
    company: Mapped[str] = mapped_column(Text, server_default=text("''"))
    title: Mapped[str] = mapped_column(Text, server_default=text("''"))
    locations: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    terms: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    source: Mapped[str] = mapped_column(Text)
    # core.near_copy.key of the text verification read; twins share it.
    near_copy_key: Mapped[str | None] = mapped_column(Text)
    # Frozen feed state: what each board's feed last said, before
    # availability moved to source_observations (catalog.AVAILABLE, stored in
    # `available`). Nothing writes it and nothing reads it; it keeps its last
    # values because data is retained (docs/agents/architecture-migration.md,
    # the jobs.active contract). A new row takes the default.
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # catalog.AVAILABLE, stored: whether some switched-on source lists it,
    # from source_observations. NULL is cannot tell (no source has observed
    # it). Written by catalog.observe and set_active for the rows they touch,
    # and reconciled over the whole catalog every hour (switches and the
    # pattern setting change it without an observation). Readers use
    # catalog.IS_AVAILABLE rather than this column directly.
    available: Mapped[bool | None] = mapped_column(Boolean)
    date_posted: Mapped[datetime.datetime | None]
    # Frozen: nothing reads or writes it (posting_uploads holds upload
    # state). It keeps the done stamp on 6,021 rows the 2026-08-24 sheet
    # import wrote and on job 11764337 (forced reparse, task 726592), recorded
    # nowhere else, so it is retained rather than dropped.
    extraction_status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class PostingUpload(Base):
    """A posting a person added by url, and where reading it stands.

    One row per upload, so the catalog row carries no per-person state. A
    forced reparse of a posting nobody uploaded writes no row: its outcome is
    its task's. Replaced jobs.uploaded_by (dropped) and jobs.extraction_status
    (frozen).
    """

    __tablename__ = "posting_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'done', 'failed')", name="ck_posting_uploads_status"
        ),
        Index("idx_posting_uploads_uploaded_by", "uploaded_by"),
    )

    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id"), primary_key=True)
    uploaded_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class PageFetch(Base):
    """One fetch of a posting's page: what came back and how it was read.

    A fact, appended and written only by core.page_fetches. The one update
    is where its text is held: inline in content, or by reference
    (content_sha256, content_object, content_object_size, with content_chars
    its length and content NULL), all set together (ck_page_fetches_reference_whole).
    page_texts is the view of the fetches that brought text back, either way.

    Ids come from ai_queries' sequence: the fetches stored there before
    2026-10-10 kept their ids when they moved, so every content_row_id that
    names one stays true.
    """

    __tablename__ = "page_fetches"
    __table_args__ = (
        CheckConstraint("status IN ('passed', 'failed')", name="ck_page_fetches_status"),
        Index("idx_page_fetches_url_id", "url", text("id DESC")),
        # The fetches page_texts' readers want: text longer than
        # core.store.MIN_CONTENT_CHARS, in either shape. Index-only for
        # CONTENT_LATERAL's id and the content backfill's gap check
        # (migrations 1276e94618f8, 7e5c9370784e): the predicate is the view's
        # condition and the lateral's, spelled as they are.
        Index(
            "idx_page_fetches_url_text",
            "url",
            text("id DESC"),
            postgresql_where=text(
                "(content <> '' OR content_object IS NOT NULL) "
                "AND COALESCE(content_chars, length(content)) > 200"
            ),
        ),
        CheckConstraint(
            "(content_object IS NULL AND content_sha256 IS NULL "
            " AND content_object_size IS NULL AND content_chars IS NULL) OR "
            "(content IS NULL AND content_sha256 IS NOT NULL "
            " AND content_object_size IS NOT NULL AND content_chars IS NOT NULL)",
            name="ck_page_fetches_reference_whole",
        ),
        Index("idx_page_fetches_created_at", "created_at"),
        # The admin ledger's filter options skip-scan distinct values through
        # ledger_rows, as they do on ai_queries (routers/admin/queries.py).
        Index("idx_page_fetches_status", "status"),
        Index("idx_page_fetches_worker_recent", "worker", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, server_default=text("nextval('ai_queries_id_seq')")
    )
    url: Mapped[str] = mapped_column(Text)
    # passed: text came back. failed: nothing usable did; the run of these is
    # what parks a posting (api.ai.verdicts.fetch_parked_sql).
    status: Mapped[str] = mapped_column(Text)
    # How the text was read: 'ats text', 'listing text', 'static', 'scraped',
    # or why it was not. Rows moved from ai_queries add two: 'verification'
    # (text older verification kept only on its answer, with that answer's id)
    # and 'unknown' (no recorded origin).
    method: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_object: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_object_size: Mapped[int | None] = mapped_column(Integer)
    content_chars: Mapped[int | None] = mapped_column(Integer)
    worker: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
