"""one application per posting, and a submitted fill points at its application

`applications` becomes the one record that a person applied. The unique
index replaces the plain one of the same name; it builds on 2,589 rows
(2026-10-10), and production held no duplicate (user_id, job_id) among the
769 rows with a job, so it cannot refuse to apply.

Data step, small enough for a migration (39 submitted fills on 2026-10-10):
a submitted fill whose posting has an application points at it (31), and a
submitted fill with no posting on the board gets an application of
provenance `apply` dated at its submit (8). Its company is the board name in
the form url, the rule `core.fetching.forms.board_of` applies to new submits.

Revision ID: 54630f36d8b8
Revises: cfe4283e671c
Create Date: 2026-10-10 01:51:36.687542

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "54630f36d8b8"
down_revision: Union[str, None] = "cfe4283e671c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("application_fills", sa.Column("application_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "application_fills_application_id_fkey",
        "application_fills",
        "applications",
        ["application_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index("idx_applications_user_job", table_name="applications")
    op.create_index(
        "idx_applications_user_job",
        "applications",
        ["user_id", "job_id"],
        unique=True,
        postgresql_where=sa.text("job_id IS NOT NULL"),
    )
    op.execute(
        """
        UPDATE application_fills f SET application_id = a.id
        FROM applications a
        WHERE f.submitted_at IS NOT NULL AND f.application_id IS NULL
          AND f.job_id IS NOT NULL AND a.user_id = f.user_id AND a.job_id = f.job_id
        """
    )
    op.execute(
        """
        WITH orphan AS (
            SELECT id, user_id, submitted_at,
                   COALESCE(substring(url from '[?&]for=([^&#]+)'),
                            substring(url from '^[a-z]+://[^/]+/([^/?#]+)')) AS board
            FROM application_fills
            WHERE submitted_at IS NOT NULL AND job_id IS NULL AND application_id IS NULL
        ), created AS (
            INSERT INTO applications (user_id, job_id, company_name, source_provenance, applied_at)
            SELECT user_id, NULL, board, 'apply', submitted_at FROM orphan ORDER BY id
            RETURNING id, user_id, applied_at
        )
        UPDATE application_fills f SET application_id = c.id
        FROM orphan o JOIN created c ON c.user_id = o.user_id AND c.applied_at = o.submitted_at
        WHERE f.id = o.id
        """
    )


def downgrade() -> None:
    op.drop_index("idx_applications_user_job", table_name="applications")
    op.create_index("idx_applications_user_job", "applications", ["user_id", "job_id"])
    op.drop_constraint("application_fills_application_id_fkey", "application_fills")
    op.drop_column("application_fills", "application_id")
