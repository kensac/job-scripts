"""public job list feedback: anonymous notes and posting reports

A visitor to a public list can leave a note about the list
(`job_list_feedback`) or report one posting on it. A posting report joins the
existing `reports` queue: `user_id` becomes nullable, and an anonymous row
names the list it came from in `managed_board_id` instead, with the optional
`contact` the visitor left. `ck_reports_has_origin` keeps every report
attributable to one or the other.

Additive for every image already running: they write `user_id` on every
report. Their report queue lists rows through an inner join on `users`, so it
skips anonymous rows rather than failing on them, while its total counts
them: until the fleet runs this release, an older API's queue can report a
total and `has_more` one page longer than the rows it returns. `reports`
held 2 rows when the test profile was measured (2026-09-04), so the
constraint's validation scan and the plain index builds are short.

Revision ID: b4d81e2f6a07
Revises: 7c3e9a1f4b20
Create Date: 2026-11-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4d81e2f6a07"
down_revision: str | None = "7c3e9a1f4b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("reports", "user_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("reports", sa.Column("managed_board_id", sa.BigInteger(), nullable=True))
    op.add_column("reports", sa.Column("contact", sa.Text(), nullable=True))
    op.create_foreign_key(
        "reports_managed_board_id_fkey",
        "reports",
        "managed_boards",
        ["managed_board_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_reports_has_origin", "reports", "user_id IS NOT NULL OR managed_board_id IS NOT NULL"
    )
    op.create_table(
        "job_list_feedback",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("managed_board_id", sa.BigInteger(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["managed_board_id"], ["managed_boards.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_job_list_feedback_status", "job_list_feedback", ["status", "id"], unique=False
    )
    op.create_index(
        "idx_job_list_feedback_created", "job_list_feedback", ["created_at"], unique=False
    )
    op.create_index(
        "idx_reports_anonymous_created",
        "reports",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("user_id IS NULL"),
    )


def downgrade() -> None:
    # An anonymous report has no user to restore NOT NULL with, and dropping
    # it would delete a visitor's report. Refuse rather than decide.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM reports WHERE user_id IS NULL) THEN
                RAISE EXCEPTION 'reports holds anonymous rows (user_id IS NULL); '
                    'downgrading would need to delete visitor reports. '
                    'Export and remove them deliberately first.';
            END IF;
        END $$
        """
    )
    op.drop_index("idx_reports_anonymous_created", table_name="reports")
    op.drop_index("idx_job_list_feedback_created", table_name="job_list_feedback")
    op.drop_index("idx_job_list_feedback_status", table_name="job_list_feedback")
    op.drop_table("job_list_feedback")
    op.drop_constraint("ck_reports_has_origin", "reports", type_="check")
    op.drop_constraint("reports_managed_board_id_fkey", "reports", type_="foreignkey")
    op.drop_column("reports", "contact")
    op.drop_column("reports", "managed_board_id")
    op.alter_column("reports", "user_id", existing_type=sa.BigInteger(), nullable=False)
