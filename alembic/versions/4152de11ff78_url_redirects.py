"""url_redirects: where a redirector's link points

Some feeds link every posting through a link shortener (go.vansh.nyc for
the vanshb03 lists), so the same posting arrived under the shortener's URL
and under the employer's, as two postings: 252 of 300 sampled short links
(2026-11-27) pointed at a posting the catalog already held. Ingest resolves
such a link to where it redirects (core.fetching.redirects) and keeps the
answer here, so a link is asked once, not on every pull.

Revision ID: 4152de11ff78
Revises: 14e68161d448
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4152de11ff78"
down_revision: str | None = "14e68161d448"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "url_redirects",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column(
            "resolved_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("url_redirects")
