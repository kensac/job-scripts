"""The postings themselves, where they were found, and what was extracted from them."""

from __future__ import annotations

import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now
from core.embeddings import EMBEDDING_DIMENSIONS


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
    # NOT "this role is open". This is feed state, and it means different
    # things by source. catalog.upsert_postings writes whatever the board last
    # said (active = EXCLUDED.active); a feed with a per-posting flag
    # accumulates false that way. For a company board that lists every open
    # posting (boards.AUTHORITATIVE), catalog.retire_unlisted also clears it
    # on every pull for rows the pull did not admit - which is the board
    # dropping the posting OR the source's title pattern no longer admitting
    # it, and only the first is a closure. Aggregator rows are never cleared
    # by absence.
    #
    # It is therefore not comparable across sources, and reading it as closure
    # has already shipped one user-facing bug: 478 applications were badged
    # "no longer live" off this flag, of which 114 had a closed-check saying
    # the posting was OPEN and 363 had never been checked at all. Exactly one
    # was backed by evidence.
    #
    # What it IS good for: every sweep and every selection gates on it, so a
    # false row costs no checks and leaves boards through demote_closed.
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
    comp_content_row_id: Mapped[int | None] = mapped_column(BigInteger)
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
    input_tokens: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    # NULL means usage or a published model price was unavailable, which must
    # stay distinct from a call that cost nothing. Ten decimal places rather than
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


class Listing(Base):
    """Every posting a board returned on its last pull, kept by the title
    pattern or not, with the text the listing call carried and the raw record
    minus that text. Refreshed per pull and aged out by
    screened_retention_days after the board stops listing it. Never read by
    visibility or the checks: a backtest or a backfill reads it so that no
    board is re-fetched and no page re-scraped for data already in hand."""

    __tablename__ = "listings"
    __table_args__ = (Index("idx_listings_source", "source", "last_seen_at"),)

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text, server_default=text("''"))
    title: Mapped[str] = mapped_column(Text, server_default=text("''"))
    locations: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    date_posted: Mapped[datetime.datetime | None]
    pattern: Mapped[str] = mapped_column(Text)
    kept: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    raw: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
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


class Location(Base):
    """One row per distinct location string a board has written, classified
    once into a place, so exclusions match places rather than text.

    71,574 location values on active postings collapse to 8,735 distinct
    strings (2026-09-04), so nearly every posting's location is a lookup here
    and only a never-seen string costs a model call. A word match on the raw
    text cannot know that "London" is the UK or "Bengaluru" is India; this
    can. Rows are data: a wrong classification is fixed by PUT
    /admin/locations/{text}, not a deploy. country is ISO 3166-1 alpha-2,
    region the US state or Canadian province code, city in English; all NULL
    when the string names no single place ("Multiple locations", "US or
    Canada"), which excludes nothing.
    """

    __tablename__ = "locations"

    # The column is named text; the attribute is not, because a class attribute
    # called text would shadow sqlalchemy.text for the columns after it.
    string: Mapped[str] = mapped_column("text", Text, primary_key=True)
    country: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    remote: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Every place the string names, [{country, region, city}], in the order
    # written. "London, Montreal, Singapore" is three; the columns above are
    # the first, for display. A criterion matches when any of them does.
    places: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    model: Mapped[str | None] = mapped_column(Text)
    classified_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)
