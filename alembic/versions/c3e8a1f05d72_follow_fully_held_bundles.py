"""follow the bundles a person holds whole

Joining a bundle used to copy its members into user_sources and remember
nothing else. A person who holds every active member of a bundle joined it
or picked the same boards one by one; the two cannot be told apart, and both
are followed here, which is the decision that joining means following.

Only a bundle held whole is followed, so no one's set of sources changes
today. A bundle held in part is not followed, even where the held part is
exactly its members at join time; whether to follow those is decided apart
(the members added since would reach the person).

Production on 2026-10-10: 2 people, 24 bundles held whole, 8 held as
exactly the members that existed at join time, 2 held in part otherwise.

Safe to run twice: ON CONFLICT DO NOTHING.

Revision ID: c3e8a1f05d72
Revises: 98ace4b3fc55
Create Date: 2026-10-10 01:20:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c3e8a1f05d72'
down_revision: Union[str, None] = '98ace4b3fc55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        WITH members AS (
            SELECT DISTINCT g.name AS group_name, s.name AS source
            FROM source_groups g
            CROSS JOIN LATERAL unnest(g.members) AS m(name)
            JOIN sources s ON s.name = m.name AND s.active
        ),
        people AS (SELECT DISTINCT user_id FROM user_sources),
        held AS (
            SELECT p.user_id, m.group_name,
                   count(us.source) AS held, count(*) AS offered
            FROM people p CROSS JOIN members m
            LEFT JOIN user_sources us ON us.user_id = p.user_id AND us.source = m.source
            GROUP BY p.user_id, m.group_name
        )
        INSERT INTO user_source_groups (user_id, group_name)
        SELECT user_id, group_name FROM held WHERE held = offered
        ORDER BY user_id, group_name
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    pass
