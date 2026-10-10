"""event answers

What a person answered about an event, append-only. Replaces
suggestion_responses (8 rows on 2026-10-10, all `accepted`), which are copied
here. action_items needs nothing copied: no person had closed or reopened an
item (all 217 closures without a settling event were the sweep's own "no
longer part of this application"). Both old tables stay until a later
migration, after every server has stopped writing them, which copies anything
answered in between and drops them.

Revision ID: 09397cf6c3d1
Revises: 98ace4b3fc55
Create Date: 2026-10-10 02:13:36.313798

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "09397cf6c3d1"
down_revision: Union[str, None] = "98ace4b3fc55"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_answers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "answer IN ('accepted', 'dismissed', 'done', 'reopened')",
            name="ck_event_answers_answer",
        ),
        sa.CheckConstraint(
            "question IN ('status', 'action')", name="ck_event_answers_question"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["event_id"], ["email_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_event_answers_event",
        "event_answers",
        ["event_id", "question", sa.literal_column("id DESC")],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO event_answers (event_id, question, answer, actor_user_id, created_at)
        SELECT sr.event_id, 'status', sr.response, sr.user_id, sr.created_at
        FROM suggestion_responses sr
        JOIN email_events e ON e.id = sr.event_id
        ORDER BY sr.id
        """
    )


def downgrade() -> None:
    op.drop_index("idx_event_answers_event", table_name="event_answers")
    op.drop_table("event_answers")
