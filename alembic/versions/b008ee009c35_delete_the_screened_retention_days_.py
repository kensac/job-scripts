"""delete the screened_retention_days config row

Revision ID: b008ee009c35
Revises: 7d32a6ece505
Create Date: 2026-10-10 15:58:04.853601

The contract half of #954, which removed the listings purge and took
screened_retention_days out of the registry: always retain all data. No code
reads the key; GET /admin/config would otherwise list a value the registry
does not describe. Merge only once every image on the fleet carries #954: an
older image that starts after this re-seeds the row, and one that runs without
it falls back to its own default of 30, so the order is about a stray row, not
about ingest.

One DELETE of at most one row. The downgrade restores nothing: an older image
re-seeds its own default at start.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b008ee009c35'
down_revision: Union[str, None] = '7d32a6ece505'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM app_config WHERE key = 'screened_retention_days'")


def downgrade() -> None:
    pass
