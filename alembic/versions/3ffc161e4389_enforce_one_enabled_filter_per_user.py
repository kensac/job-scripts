"""enforce one enabled filter per user

Revision ID: 3ffc161e4389
Revises: e5f6a7b8c9d0
Create Date: 2026-09-09 03:43:34.372917

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3ffc161e4389"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enabled filters are a person's choices. Refuse ambiguous existing data
    # rather than silently picking which prompt survives deployment.
    op.execute("LOCK TABLE user_filters IN SHARE MODE")
    op.execute("""
        DO $$
        DECLARE conflicting_users bigint;
        BEGIN
            SELECT count(*) INTO conflicting_users FROM (
                SELECT user_id FROM user_filters WHERE enabled
                GROUP BY user_id HAVING count(*) > 1
            ) conflicts;
            IF conflicting_users > 0 THEN
                RAISE EXCEPTION
                    'Cannot enforce one enabled filter: % users have multiple enabled filters. Resolve their choices explicitly before retrying.',
                    conflicting_users;
            END IF;
        END $$
    """)
    op.create_index(
        "uq_user_filters_one_enabled",
        "user_filters",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_user_filters_one_enabled",
        table_name="user_filters",
        postgresql_where=sa.text("enabled"),
    )
