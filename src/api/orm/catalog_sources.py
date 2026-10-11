"""Source configuration, listing evidence, and source observations."""

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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now


class JobListingEvent(Base):
    """A feed said this posting was listed, or stopped saying so.

    Append only. `jobs.active` is the current answer and this is how it got
    there, which is a different question and the one nothing could answer: a
    posting that drops off a board and returns leaves no trace in a boolean,
    so a closed verdict stayed permanent (#504) and, once that was fixed by
    re-checking on a return, nothing measured how often a board returns the
    same posting. A board that flaps bills a re-check every time.

    Written only on a change. A pull that says what the last pull said is not
    an observation worth a row, and at 74,000 postings an hour it would be
    millions of rows a day saying nothing.

    The same shape as email_events and application_matches: latest row wins on
    read, and the index is (job_id, id DESC) for exactly that.
    """

    __tablename__ = "job_listing_events"
    __table_args__ = (Index("idx_job_listing_events_latest", "job_id", text("id DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    # Which feed said so. A posting can be carried by more than one.
    source: Mapped[str] = mapped_column(Text)
    # True: the pull listed and admitted it. False: the pull did not, and the
    # board is authoritative, so absence is the board dropping it.
    listed: Mapped[bool] = mapped_column(Boolean)
    at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class SourceObservation(Base):
    """What one source said about one posting, when it changed. Phase 3 of
    docs/agents/architecture-migration.md.

    One url is one job; each source that lists it observes it separately, so
    a posting carried by a company board and an aggregator has a history per
    source. Written by catalog.observe after every pull, only where the
    source's latest row for the job says something else. Never updated,
    never deleted: availability is a projection over the latest row per
    (job, source) and can always be recomputed from here.

    `job_listing_events` is not this. It logs flips of jobs.active, so a
    second source listing an already active posting leaves no row there,
    and its false rows do not say whether the board dropped the posting,
    the pattern stopped admitting it, or the source was switched off.
    """

    __tablename__ = "source_observations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('appeared', 'reappeared', 'filtered', 'unlisted', 'not_listed')",
            name="ck_source_observations_kind",
        ),
        # The pull's change check: the latest row per job of one source.
        Index("idx_source_observations_by_source", "source", "job_id", text("id DESC")),
        # Availability: the latest row per source of one job.
        Index("idx_source_observations_by_job", "job_id", "source", text("id DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(Text)
    # appeared: first listed and admitted by this source. reappeared: listed
    # and admitted again after any other kind. filtered: listed, and the
    # source's title pattern does not admit it. unlisted: the source says it
    # is no longer listed, by leaving it out of a complete pull of a board
    # that lists every open posting (boards.AUTHORITATIVE) or by its own
    # record's inactive flag. not_listed: left out of an aggregator's pull,
    # which says nothing about whether it closed.
    kind: Mapped[str] = mapped_column(Text)
    # The ingest_source task whose pull said so. Tasks are never pruned.
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class TitlePattern(Base):
    """Each distinct source title pattern, stored once. A listing points at
    the pattern that judged its kept flag instead of carrying a copy: 1,060,806
    listings rows held 4 distinct patterns, 492 MB of the table's 863 MB
    (2026-10-10). Keyed by the SHA-256 of the text because a btree entry
    cannot hold a long pattern; core.catalog.title_pattern_id accepts a digest
    match only when the text is equal. Never updated or deleted."""

    __tablename__ = "title_patterns"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    pattern: Mapped[str] = mapped_column(Text)


class Listing(Base):
    """Every posting a board has returned, kept by the title pattern or
    not, with the text the listing call carried and the raw record minus that
    text. Rewritten by a pull only when it changed or its last_seen_at is
    older than listings_seen_refresh_hours. Never deleted: a row the board
    stopped listing stays, and core.catalog.LISTED_NOW says which rows are
    on their source's latest pull. Never read by
    visibility or the checks: a backtest or a backfill reads it so that no
    board is re-fetched and no page re-scraped for data already in hand."""

    __tablename__ = "listings"
    # source alone, not (source, last_seen_at): an index on a column every
    # refresh moves makes every update a new index entry in every index
    # (860 HOT of 1.17M updates a day, 2026-10-03). Every reader filters by
    # source, and LISTED_NOW reads a source's newest last_seen_at through it.
    # Fillfactor is in 507fe2f38949.
    __table_args__ = (
        Index("idx_listings_by_source", "source"),
        *(
            # A field is held by reference exactly when all three of its
            # reference columns are set: the value's own SHA-256 (the bundle
            # member's name and the upsert's change check), the bundle's
            # SHA-256 and the bundle's size. NOT VALID: every existing row is
            # inline, and the check binds every row written from here on.
            CheckConstraint(
                f"({field}_sha256 IS NULL) = ({field}_object IS NULL) "
                f"AND ({field}_object IS NULL) = ({field}_object_size IS NULL)",
                name=f"ck_listings_{field}_reference",
                postgresql_not_valid=True,
            )
            for field in ("description", "raw")
        ),
    )

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text, server_default=text("''"))
    title: Mapped[str] = mapped_column(Text, server_default=text("''"))
    locations: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    date_posted: Mapped[datetime.datetime | None]
    # The title pattern whose match set kept, stored once (TitlePattern).
    pattern_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("title_patterns.id", name="fk_listings_pattern")
    )
    kept: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    raw: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # A value held by reference lives in a verified bundle object
    # (core.listing_payloads); the inline column then holds its default.
    description_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    description_object: Mapped[bytes | None] = mapped_column(LargeBinary)
    description_object_size: Mapped[int | None] = mapped_column(Integer)
    raw_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    raw_object: Mapped[bytes | None] = mapped_column(LargeBinary)
    raw_object_size: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class Source(Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    listings_url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
    # The employer a company board belongs to; NULL for an aggregator, whose
    # rows name it. Required for the boards that never say (core/boards.py).
    company: Mapped[str | None] = mapped_column(Text)
    # Case-insensitive regex a title must match to be ingested; NULL takes all.
    title_pattern: Mapped[str | None] = mapped_column(Text)
    # Hours between pulls; 1 is the hourly cycle. A bundle of a few hundred
    # company boards is set to 24 in one write through the category switch.
    ingest_interval_hours: Mapped[int] = mapped_column(Integer, server_default=text("1"))


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


class UserSourceGroup(Base):
    """A person following a bundle: every active member reaches them, the
    ones added later included. `user_sources` holds what they picked one by
    one; the view `user_source_set` is the two together, and is what every
    reader reads (api/source_selection.py)."""

    __tablename__ = "user_source_groups"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_name: Mapped[str] = mapped_column(
        Text, ForeignKey("source_groups.name", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


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
