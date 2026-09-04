"""user_filters.preset_id: which preset a filter was adopted from

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-09-04

A filter adopted from a preset carried no record of it, so the only way to
tell was the name, and a renamed adopted filter read as never adopted: the
page offered "Add" again and the second adopt was refused as ALREADY_ADOPTED
(by name) forever. Nullable, so a filter written by hand stays unattributed;
SET NULL on preset delete, so retiring a preset never touches a user's
filter. Backfilled once by today's rule (same name as an active preset),
which is the best provenance that exists for rows adopted before this.
"""

import sqlalchemy as sa
from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_filters", sa.Column("preset_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "user_filters_preset_id_fkey",
        "user_filters",
        "filter_presets",
        ["preset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE user_filters uf SET preset_id = p.id
        FROM filter_presets p
        WHERE p.name = uf.name AND uf.preset_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("user_filters_preset_id_fkey", "user_filters", type_="foreignkey")
    op.drop_column("user_filters", "preset_id")
