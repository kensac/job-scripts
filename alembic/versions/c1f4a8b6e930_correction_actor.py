"""who corrected it: actor_user_id on the two append-only logs

Revision ID: c1f4a8b6e930
Revises: a7f3c9e1d582
Create Date: 2026-09-03 09:00:00.000000

The admin is about to get correction tools that act on OTHER USERS' data -
friends and family, not a hypothetical. Today a human correction is told from a
model's only by `model IS NULL`, which cannot tell the owner's own correction
from an administrator's, and after the fact it never will: both logs are
append-only, so attribution that was not written at the time cannot be
reconstructed from them.

A user id rather than a role enum. Whether a correction was the owner's or an
admin's is DERIVED by comparing this against the row's owner, so there is no
second copy of that fact to drift - the same reason `needs_reconnect` is read
off `invalid_at` rather than stored beside it.

NULL means no person: the matcher or the classifier wrote it. Together with
`model` that gives the full provenance of every row - `model` says which
machine, `actor_user_id` says which human, and neither being set has never
happened and would be a bug.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c1f4a8b6e930"
down_revision: Union[str, None] = "a7f3c9e1d582"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("application_matches", "email_events"):
        op.add_column(
            table,
            sa.Column(
                "actor_user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    # Deleting the admin must not erase the fact that a correction was made,
    # only who made it - hence SET NULL rather than CASCADE. A cascade here
    # would delete evidence out of an append-only log.


def downgrade() -> None:
    for table in ("application_matches", "email_events"):
        op.drop_column(table, "actor_user_id")
