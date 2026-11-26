"""app_config: retire local_embed_per_cycle

Revision ID: a4c9e2f7b318
Revises: f6b2d8e14a37
Create Date: 2026-11-26

The local embedding sweep now covers every posting and a run drains its
backlog batch by batch, so a run's size is no longer a per-cycle budget to
tune: its batch is tasks.derivations.local_embeddings.BATCH_POSTINGS, sized
by the text a batch holds. The setting's stored row goes with its key.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a4c9e2f7b318"
down_revision: str | None = "f6b2d8e14a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM app_config WHERE key = 'local_embed_per_cycle'")


def downgrade() -> None:
    # The key's declared default is its value when no row exists (db.get_config).
    pass
