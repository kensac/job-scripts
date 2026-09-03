"""keep the html a message arrived as

Revision ID: b8e4f1a06c93
Revises: c41d7e9a20b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e4f1a06c93"
down_revision: str | None = "c41d7e9a20b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive and nullable: every existing row keeps working with it NULL,
    # which is the honest value for a message imported before the markup was
    # kept. The backfill task fills it where the markup is still recoverable
    # from body_text; nothing else can recover it, because the import streams
    # the archive and retains no copy.
    op.add_column("email_messages", sa.Column("body_html", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("email_messages", "body_html")
