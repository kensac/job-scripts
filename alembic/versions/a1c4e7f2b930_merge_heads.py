"""merge the two heads that landed in parallel

Revision ID: a1c4e7f2b930
Revises: d4a9c1e7b358, e8b2c4d9f731

Two migrations were written against the same parent by different sessions and
merged within minutes of each other: prompt provenance and the fleet usage
rows. Neither is wrong and they do not touch the same tables - alembic simply
cannot pick a head when two exist, so `upgrade head` fails and nothing
migrates.

This carries no schema change. It exists to say the two branches are the same
history from here on.
"""

from __future__ import annotations

revision = "a1c4e7f2b930"
down_revision = ("d4a9c1e7b358", "e8b2c4d9f731")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
