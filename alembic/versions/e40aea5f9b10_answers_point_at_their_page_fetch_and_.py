"""answers point at their page fetch and model call

An answer in ai_queries carries a copy of what it judged (input_content: the
page, wrapped with the company and title for a custom filter) and the usage
of the call that produced it. Both are copies: the page is a page_fetches row
and the call is a model_calls row. These columns let an answer point at them
instead, so the copies can be cleared and dropped by later releases.

model_calls.duration_ms carries the wall time of a live call, which 97,397
live verdicts held on 2026-10-10 and nothing else does.

Nullable columns with no default: catalog only, no row is rewritten.

Revision ID: e40aea5f9b10
Revises: 1276e94618f8
Create Date: 2026-10-10 14:22:24.168053

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e40aea5f9b10"
down_revision: str | None = "1276e94618f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column("ai_queries", sa.Column("page_fetch_id", sa.BigInteger(), nullable=True))
    op.add_column("ai_queries", sa.Column("model_call_id", sa.BigInteger(), nullable=True))
    op.add_column("model_calls", sa.Column("duration_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.drop_column("model_calls", "duration_ms")
    op.drop_column("ai_queries", "model_call_id")
    op.drop_column("ai_queries", "page_fetch_id")
