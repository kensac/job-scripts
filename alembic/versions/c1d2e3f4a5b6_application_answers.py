"""application answers: a person's resumes and writing style, the questions a form asks, the drafts

Revision ID: c1d2e3f4a5b6
Revises: f9a0b1c2d3e4
Create Date: 2026-09-06

Browser autofill stops at the free-response box. The questions themselves are
public on four of the ATSs the board reads (Greenhouse, Ashby, Lever and
Workable expose the form without a sign-in), so the form is read once per
posting and cached by url; the answer is per person and per job, written from
a resume they keep here and in a style they describe in their own words.
Additive.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c1d2e3f4a5b6"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("writing_style", sa.Text(), nullable=True))
    op.create_table(
        "user_resumes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.UniqueConstraint("user_id", "name"),
    )
    # One row per posting url, whoever asked. questions is NULL when the host
    # is one this code cannot read (the form sits behind a sign-in, or the
    # ATS publishes none); error says why the last read failed.
    op.create_table(
        "application_forms",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("questions", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_table(
        "application_answers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        # form: read off the ATS; manual: pasted in by the person, for the
        # hosts whose forms cannot be read.
        sa.Column("source", sa.Text(), nullable=False, server_default="form"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("draft", sa.Text(), nullable=True),
        # The back-and-forth: [{"role": "user"|"assistant", "text", "at"}].
        sa.Column("turns", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.UniqueConstraint("user_id", "job_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("application_answers")
    op.drop_table("application_forms")
    op.drop_table("user_resumes")
    op.drop_column("user_settings", "writing_style")
