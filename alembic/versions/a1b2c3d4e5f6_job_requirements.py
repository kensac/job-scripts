"""job_requirements, job_skills, user_settings.background

Revision ID: a1b2c3d4e5f6
Revises: a7c31f9e2b48
Create Date: 2026-09-01 23:50:00.000000

ai_queries holds the scraped text of 20,730 distinct postings, most of them now
closed and unscrapable, and nothing has ever read it for anything but a
pass/fail verdict. These tables hold what that text actually says a role
requires, so the corpus can answer "what does this market want, and what am I
missing" rather than only "is this one still open".

Keyed by url, not by job id, and with no foreign key to jobs. 5,511 of the
20,680 urls with usable content have no jobs row at all; those are exactly the
postings that are gone for good, and a job-keyed table would discard 27% of the
asset. It is the same reasoning that keeps ai_queries url-keyed: this is a cache
of paid AI work whose lifetime is deliberately independent of any job row.

Skills get a row each rather than an array column because the product question
is a GROUP BY over a filtered slice, which an index answers and an array does
not. skill_raw is kept beside the canonical skill so a better normalisation is
an UPDATE over stored values instead of another paid extraction pass.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'a7c31f9e2b48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_requirements',
        sa.Column('url', sa.Text(), primary_key=True),
        sa.Column('has_requirements', sa.Boolean(), nullable=False),
        # NULL means the posting does not say, which is a different fact from
        # zero years or no degree required, and the one the aggregate turns on.
        sa.Column('yoe_min', sa.Integer(), nullable=True),
        sa.Column('yoe_max', sa.Integer(), nullable=True),
        sa.Column('degree_min', sa.Text(), nullable=True),
        sa.Column('degree_required', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('degree_fields', postgresql.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column('enrollment_required', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('seniority', sa.Text(), nullable=True),
        sa.Column('employment_type', sa.Text(), nullable=True),
        sa.Column('clearance', sa.Text(), nullable=True),
        sa.Column('citizenship_required', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('sponsorship', sa.Text(), nullable=True),
        sa.Column('model', sa.Text(), nullable=True),
        # The page text this row was read from. A posting that is re-scraped
        # can then be told from one that never changed, without re-running the
        # extraction to find out.
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('extracted_at', postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    # The market aggregates all slice on these, and a sequential scan of 20k
    # rows per request would be paid on every page load.
    op.create_index('idx_job_requirements_seniority', 'job_requirements', ['seniority'])
    op.create_index('idx_job_requirements_employment', 'job_requirements', ['employment_type'])

    op.create_table(
        'job_skills',
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('skill', sa.Text(), nullable=False),
        sa.Column('skill_raw', sa.Text(), nullable=False),
        # On the raw text, not the canonical form: two raw spellings collapsing
        # onto one canonical skill is normal and must not be a conflict, while
        # the same raw string twice in one list is a duplicate.
        sa.PrimaryKeyConstraint('url', 'kind', 'skill_raw'),
    )
    op.create_index('idx_job_skills_skill', 'job_skills', ['skill', 'kind'])

    op.add_column(
        'user_settings',
        sa.Column('background', postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column('user_settings', 'background')
    op.drop_index('idx_job_skills_skill', table_name='job_skills')
    op.drop_table('job_skills')
    op.drop_index('idx_job_requirements_employment', table_name='job_requirements')
    op.drop_index('idx_job_requirements_seniority', table_name='job_requirements')
    op.drop_table('job_requirements')
