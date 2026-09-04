"""drop user_settings.background

Revision ID: 3c1d5a90f2e7
Revises: 2b80daf7ac80
Create Date: 2026-09-03

NON-ADDITIVE. This drops a column, and migrations.md requires that be
announced to the deployment owner before it merges rather than after. It was
(#301).

The column held one JSONB profile per user - years, degree, skills, clearance,
sponsorship - and fed exactly one reader, `GET /requirements/gap`, which is
deleted in the same change. It is `{}` for the only user in production
(measured 2026-09-03), so nothing is being discarded here; the step that
collected it never produced data.

It is dropped rather than left in place because it was the wrong shape, not
merely unused: a single background is a ceiling on someone who wants backend
and frontend roles at once, and filters are already per profile. Leaving an
empty column invites the same idea back.

The safe window is small but real: a container still running the old code
selects `background` in `GET /user/settings` and would 500 between this
migration applying and that container being replaced. A deploy recreates
containers, so that window is the length of one replacement, on one route,
for one user.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '3c1d5a90f2e7'
down_revision: Union[str, None] = '2b80daf7ac80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('user_settings', 'background')


def downgrade() -> None:
    # Recreated empty. The values are not recoverable and were not worth
    # recovering: every row held `{}`.
    op.add_column(
        'user_settings',
        sa.Column('background', postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )
