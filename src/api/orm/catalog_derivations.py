"""Derived posting answers, skill rows, and classified places."""

from __future__ import annotations

import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.orm.base import Base, _now
from core.derivations.embeddings import EMBEDDING_DIMENSIONS


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
    # The page fetch the answer was read from (a page_texts id). Ids are not
    # TOASTed, so "is there a newer page for this url" is an index read rather
    # than a detoast of the corpus; the hash above then decides whether the
    # text actually changed and the work needs paying for again.
    content_row_id: Mapped[int | None] = mapped_column(BigInteger)
    extracted_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class JobComp(Base):
    """The pay a posting's page states, read by the comp derivation
    (tasks.derivations.compensation). A row means the page was read; NULL amounts mean it states
    none. Keyed by url with no foreign key, like the other derived tables: a
    cache of paid work outlives the job row.

    Amounts are yearly (core.derivations.compensation.PERIOD_TO_YEARLY); `comp_period`,
    `comp_currency` and `comp_basis` say what was advertised, so the figure can
    be re-derived and audited.
    """

    __tablename__ = "job_comp"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    comp_min: Mapped[int | None] = mapped_column(BigInteger)
    comp_max: Mapped[int | None] = mapped_column(BigInteger)
    comp_text: Mapped[str | None] = mapped_column(Text)
    comp_period: Mapped[str | None] = mapped_column(Text)
    comp_currency: Mapped[str | None] = mapped_column(Text)
    comp_basis: Mapped[str | None] = mapped_column(Text)
    # NULL on answers copied from jobs, which never recorded them.
    model: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    # The page fetch the answer was read from. NULL on 62,004 answers from
    # before fetches were recorded (2026-10-10); those are never re-read.
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
    # The page fetch the vector was read from, as on JobRequirements.
    content_row_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


class JobProfile(Base):
    """Versioned shadow classification of one exact content observation."""

    __tablename__ = "job_profiles"
    __table_args__ = (
        UniqueConstraint(
            "content_row_id", "classifier_version", "model", name="uq_job_profiles_derivation"
        ),
        CheckConstraint("cardinality(role_tracks) <= 2", name="ck_job_profiles_two_tracks"),
        Index("idx_job_profiles_url_classified", "url", text("classified_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    # The page fetch the profile was read from. For 3,502 of 84,052 profiles
    # (2026-10-10) that is a 'verification' fetch: text older verification
    # kept on its answer, moved into page_fetches under the answer's id. No
    # foreign key, like content_row_id on job_requirements and job_embeddings;
    # a profile whose row is not a url's newest fetch reads as superseded.
    content_row_id: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(Text)
    classifier_version: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    primary_role_family: Mapped[str] = mapped_column(Text)
    role_tracks: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    career_stage: Mapped[str] = mapped_column(Text)
    employment_type: Mapped[str] = mapped_column(Text)
    organization_sector: Mapped[str] = mapped_column(Text)
    people_manager: Mapped[bool | None] = mapped_column(Boolean)
    company_selectivity: Mapped[str] = mapped_column(Text)
    role_selectivity: Mapped[str] = mapped_column(Text)
    classified_at: Mapped[datetime.datetime] = mapped_column(server_default=_now)


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
