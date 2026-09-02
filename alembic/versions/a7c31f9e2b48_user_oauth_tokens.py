"""user_oauth_tokens: per-user external provider credentials

Revision ID: a7c31f9e2b48
Revises: d5e2a91c7f38
Create Date: 2026-09-01 12:00:00.000000

The system captures nothing about what comes back from an application - no
interviews, no rejections. Reading the user's mailbox closes that loop, and
this is where the credential to do it lives.

Keyed on (user_id, provider) rather than user_id alone even though Google is
the only provider today. An app in Testing mode with a restricted scope gets a
refresh token that dies after seven days, so the two documented escapes - a
verified production OAuth app, or IMAP with an app password - are both live
possibilities. Making provider part of the key means either one is a new row
value instead of a migration.

No status column. The one fact worth persisting is that the provider rejected
our refresh token, which is invalid_at; "needs reconnect" is that column being
non-NULL, derived at read time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA

revision: str = 'a7c31f9e2b48'
down_revision: Union[str, None] = 'd5e2a91c7f38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_oauth_tokens',
        sa.Column(
            'user_id', sa.BigInteger(),
            sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True,
        ),
        sa.Column('provider', sa.Text(), primary_key=True),
        sa.Column('refresh_token_enc', BYTEA(), nullable=False),
        sa.Column('access_token_enc', BYTEA(), nullable=True),
        sa.Column('access_token_expires_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            'scopes', ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'"),
        ),
        sa.Column('account_email', sa.Text(), nullable=True),
        sa.Column('invalid_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('invalid_reason', sa.Text(), nullable=True),
        sa.Column(
            'connected_at', sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text('now()'),
        ),
        sa.Column(
            'updated_at', sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text('now()'),
        ),
    )


def downgrade() -> None:
    op.drop_table('user_oauth_tokens')
