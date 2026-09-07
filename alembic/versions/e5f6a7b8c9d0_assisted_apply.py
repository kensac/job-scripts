"""assisted apply: a person's profile, the answers they have given before, and every form filled

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-07

The drafts cover the free-response box; everything else on an application
form is the same twenty facts asked in a hundred phrasings. A browser
extension reads the form, asks the API what goes in each field, fills it,
and stops short of submit. The profile holds the facts; the answer bank
holds what the person typed into a field the profile could not fill, so the
next form with that label is filled from it; the fills ledger records every
field of every form, which rung filled it and what the person changed, so
what to improve next is read off a table rather than guessed. A report is
the page as the extension saw it when something went wrong, with the
person's note, for triage later. The resume's bytes are kept so the
extension can attach the file. Additive.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "profile",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("user_resumes", sa.Column("pdf", postgresql.BYTEA(), nullable=True))
    op.create_table(
        "application_answer_bank",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("label_norm", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("times_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.UniqueConstraint("user_id", "label_norm"),
    )
    op.create_table(
        "application_fills",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "application_fills_user_created", "application_fills", ["user_id", "created_at"]
    )
    op.create_table(
        "application_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("page", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("application_reports")
    op.drop_table("application_fills")
    op.drop_table("application_answer_bank")
    op.drop_column("user_resumes", "pdf")
    op.drop_column("user_settings", "profile")
