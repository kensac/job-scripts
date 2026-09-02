"""task_model_overrides: the model a person chose for a task, append-only

Revision ID: e5b3d7c2f194
Revises: d4a9c1e7b358
Create Date: 2026-09-02 12:10:00.000000

Which model runs a task is currently a decision made in code, at the call site,
by whoever wrote it. This lets the owner of the system change it without a
deploy, and records who changed it, when, and to what.

APPEND-ONLY, latest row per purpose wins - the same shape ai_queries uses for
verdicts, and for the same reason. A settings table that overwrote itself would
answer "what is the model now" and lose "what was it before and when did that
change", which is the half a monthly review actually needs: a quality
regression is noticed weeks after the switch that caused it.

`model` is nullable, and a NULL row is how an override is CLEARED. Deleting the
row instead would erase the fact that an override once existed, which is the
same information loss in a different place.

The purpose column is the task's own key from its TaskShape, which is also what
the spend ledger and ai_batches group by. One key, so the thing you configure
and the thing you read spend for cannot drift apart.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e5b3d7c2f194'
down_revision: Union[str, None] = 'd4a9c1e7b358'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'task_model_overrides',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('purpose', sa.Text(), nullable=False),
        # NULL means "cleared, fall back to what the call site sanctioned".
        sa.Column('model', sa.Text(), nullable=True),
        # Whether the chosen model was outside the call site's own candidates,
        # recorded at the time of the decision rather than re-derived later:
        # the sanctioned set lives in code and moves, so a row that only stored
        # the model could not say afterwards whether it was an override.
        sa.Column('overrode_sanctioned', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('changed_by', sa.BigInteger(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    # Serves both the hot read - latest row for one purpose, on every batch -
    # and the history view.
    op.create_index(
        'idx_task_model_overrides_purpose', 'task_model_overrides', ['purpose', 'id']
    )


def downgrade() -> None:
    op.drop_index('idx_task_model_overrides_purpose', table_name='task_model_overrides')
    op.drop_table('task_model_overrides')
