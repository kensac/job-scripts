"""tasks.parent_id as a real column

Revision ID: b3f8d21c5a04
Revises: a7c1e9d40b22
Create Date: 2026-09-01 06:05:00.000000

Chunk tasks recorded their parent in payload->>'parent_id'. No index can serve
that expression, so every lookup seq-scanned the whole tasks table - which is
append-only and never pruned, so the cost grew with lifetime task count rather
than with live work. pg_stat_user_tables showed 7,977 sequential scans reading
15.87M tuples, and _update_parent_progress runs once per five completed jobs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b3f8d21c5a04'
down_revision: Union[str, None] = 'a7c1e9d40b22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('parent_id', sa.BigInteger(), nullable=True))
    op.execute(
        "UPDATE tasks SET parent_id = (payload->>'parent_id')::bigint "
        "WHERE payload ? 'parent_id' AND payload->>'parent_id' ~ '^[0-9]+$'"
    )
    op.create_index('idx_tasks_parent', 'tasks', ['parent_id', 'status'],
                    postgresql_where=sa.text('parent_id IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('idx_tasks_parent', table_name='tasks')
    op.drop_column('tasks', 'parent_id')
