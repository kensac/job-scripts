"""merge b8e4f1a06c93 and c1f4a8b6e930

Revision ID: d9f2c4b81e57
Revises: b8e4f1a06c93, c1f4a8b6e930
Create Date: 2026-09-03 08:20:00.000000

Two migrations chained off c41d7e9a20b8 independently and both merged, which
left main with two alembic heads. `alembic upgrade head` refuses to choose
between them, so init_schema() raises at startup - every worker and the API
call it, and CI calls it at conftest import.

It looked fine because a host with alembic_version already populated does not
re-run the upgrade. It breaks on a fresh host, a rebuilt test database, or any
container restarting into init_schema. That is the whole failure: not wrong
data, an application that will not start.

No schema change. The two are independent - one adds email_messages.body_html,
the other adds actor_user_id to the correction tables - so the order they are
applied in does not matter, which is what makes a plain merge the right repair
rather than a rebase of one onto the other.
"""
from typing import Sequence, Union

revision: str = 'd9f2c4b81e57'
down_revision: Union[str, Sequence[str], None] = ('b8e4f1a06c93', 'c1f4a8b6e930')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
