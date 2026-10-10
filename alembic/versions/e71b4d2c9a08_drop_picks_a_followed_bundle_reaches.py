"""drop picks a followed bundle reaches

Joining a bundle copied its members into user_sources; while servers from
before the follow model ran, the copy was how they saw the bundle. Joining no
longer copies. This removes the copies already made: every pick that a bundle
the person follows also reaches. No one's set of sources changes, because the
follow reaches each of them. A pick made one by one that a followed bundle
also reaches cannot be told apart from a copy and is removed with them; it
stays in the person's set through the follow.

Applies only after every server reads user_source_set: a server that reads
user_sources alone would lose these boards.

Production on 2026-10-10, with the 24 follows of c3e8a1f05d72: 3,219 of
4,592 picks are reached by a followed bundle.

Safe to run twice: the second run finds nothing to remove.

Revision ID: e71b4d2c9a08
Revises: c3e8a1f05d72
Create Date: 2026-10-10 01:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'e71b4d2c9a08'
down_revision: Union[str, None] = 'c3e8a1f05d72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM user_sources p
        USING user_followed_sources f
        WHERE f.user_id = p.user_id AND f.source = p.source
        """
    )


def downgrade() -> None:
    # The removed picks are still reached through the follows; a server from
    # before this reads user_source_set too.
    pass
