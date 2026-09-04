"""drop user_settings.identities and identities_confirmed_at

Revision ID: 7f2b41c8ae03
Revises: 3c1d5a90f2e7
Create Date: 2026-09-03

NON-ADDITIVE, announced before merge like its parent.

These two columns stored the answer to a settings screen asking the user which
email addresses were theirs. Both are NULL for the only user in production
(measured 2026-09-03), so the confirmation branch in `identities_for` had never
once been taken.

WHAT IS NOT BEING REMOVED, because the names are close enough to confuse: the
self-sent guard stays, and so does the derivation that feeds it
(core/identity.py, `identities_for`, `_SELF_SENT`, `_heal_self_sent`). That
mechanism has booked 1,028 corrections on this corpus and it runs off the
mailbox itself, needing nobody to confirm anything. What goes is the manual
override on top of it, which never overrode anything.

Chained onto 3c1d5a90f2e7 rather than branched from the same parent: two
migrations off one parent is two heads, and `upgrade head` then fails only
once both are on main, on new hosts, while running hosts look healthy.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '7f2b41c8ae03'
down_revision: Union[str, None] = '3c1d5a90f2e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('user_settings', 'identities')
    op.drop_column('user_settings', 'identities_confirmed_at')


def downgrade() -> None:
    # Recreated empty, which is what they held.
    op.add_column('user_settings', sa.Column('identities', postgresql.JSONB(), nullable=True))
    op.add_column(
        'user_settings',
        sa.Column('identities_confirmed_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )
