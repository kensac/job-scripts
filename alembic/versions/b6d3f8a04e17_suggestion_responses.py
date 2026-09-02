"""what the user did about a suggestion

Revision ID: b6d3f8a04e17
Revises: e5b3d7c2f194
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b6d3f8a04e17"
down_revision = "e5b3d7c2f194"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Suggestions themselves are DERIVED, never stored: they are a read-time
    # comparison of what the mail says against what the board says, so they
    # correct themselves when either side changes. Only the user's answer is a
    # fact, and only the answer is kept.
    #
    # Keyed on the event as well as the application, so a dismissal silences
    # THIS evidence rather than the question. A later rejection from the same
    # company is new evidence and gets asked again.
    op.create_table(
        "suggestion_responses",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.BigInteger(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("suggested_status", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_suggestion_responses_app",
        "suggestion_responses",
        ["application_id", "event_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_suggestion_responses_app", table_name="suggestion_responses")
    op.drop_table("suggestion_responses")
