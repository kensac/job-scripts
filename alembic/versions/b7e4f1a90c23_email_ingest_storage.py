"""email ingest: messages, events, applications, matches, action items

Revision ID: b7e4f1a90c23
Revises: a1b2c3d4e5f6
Create Date: 2026-09-02 04:45:00.000000

716 applications since May 2025 and not one recorded outcome - no interview,
no rejection, no offer. The evidence exists, in Gmail. These tables are where
it lands.

The load-bearing decision is that an APPLICATION DOES NOT REQUIRE A JOB.
`user_jobs` is keyed on job_id and structurally cannot represent "I applied to
Acme in 2022 and got rejected", which is most of the Outlook-era archive: those
postings were never in this catalog and never can be. The email still carries
company, title, dates and the outcome, and that is a real application.

Deliberately NOT done: synthesising a jobs row from an email. It would make
everything downstream uniform, which is the temptation, and it would fill the
catalog with rows that were never scraped, never verified, and can never be
re-checked. The catalog stays "postings we actually saw".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'b7e4f1a90c23'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'email_messages',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('user_id', sa.BigInteger(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        # Stable per provider. Gmail ids survive across Takeout and the API, so
        # this is what stops the four .olm archives (which overlap heavily with
        # each other and with Takeout) importing the same message four times.
        sa.Column('provider_message_id', sa.Text(), nullable=False),
        sa.Column('provider_thread_id', sa.Text(), nullable=True),
        # Where this copy came from. Kept because an olm-sourced message may be
        # thinner than the same message from the API, and a later re-import
        # should be able to prefer the better copy.
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('from_email', sa.Text(), nullable=True),
        sa.Column('from_name', sa.Text(), nullable=True),
        sa.Column('to_emails', sa.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('sent_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('body_text', sa.Text(), nullable=True),
        sa.Column('headers', JSONB(), nullable=True),
        # The cheap deterministic verdict, recorded rather than applied and
        # forgotten: the pre-filter is recall-first and will be widened, and a
        # stored verdict is what lets a widening re-sweep only what it
        # previously dropped instead of reclassifying 38,685 messages again.
        sa.Column('prefilter_hit', sa.Boolean(), nullable=True),
        sa.Column('prefilter_reason', sa.Text(), nullable=True),
        sa.Column('imported_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('user_id', 'provider_message_id', name='uq_email_messages_provider_id'),
    )
    op.create_index('idx_email_messages_thread', 'email_messages',
                    ['user_id', 'provider_thread_id'])
    op.create_index('idx_email_messages_sent', 'email_messages', ['user_id', 'sent_at'])
    # Partial: the sweeps only ever ask for messages the pre-filter kept.
    op.create_index('idx_email_messages_candidates', 'email_messages', ['user_id', 'id'],
                    postgresql_where=sa.text('prefilter_hit'))

    op.create_table(
        'applications',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('user_id', sa.BigInteger(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        # NULLABLE, and that is the point of this table existing. A 2022
        # application has no posting in the catalog and never will.
        sa.Column('job_id', sa.BigInteger(),
                  sa.ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True),
        # Carried directly for the job-less case. Provenance says whether these
        # came from a matched posting or were read off an email, because
        # "company: Acme" inferred from a sender domain is weaker evidence than
        # a matched posting and the UI has to be able to say so.
        sa.Column('company_name', sa.Text(), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('source_provenance', sa.Text(), nullable=False, server_default='email'),
        sa.Column('applied_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('idx_applications_user_job', 'applications', ['user_id', 'job_id'])
    op.create_index('idx_applications_company', 'applications',
                    ['user_id', sa.text('lower(company_name)')])

    op.create_table(
        'email_events',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('message_id', sa.BigInteger(),
                  sa.ForeignKey('email_messages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Text(), nullable=True),
        sa.Column('occurred_at', sa.TIMESTAMP(timezone=True), nullable=True),
        # Deadlines lifted from prose are guesses. Nothing may auto-fail on one,
        # so the flag travels with the value rather than being inferred later.
        sa.Column('deadline_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('deadline_inferred', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('detail', JSONB(), nullable=True),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text('now()')),
    )
    # Append-only, latest row per (message, kind) wins - the same rule as the
    # verdict log, so re-classifying appends rather than destroying what the
    # previous pass concluded.
    op.create_index('idx_email_events_latest', 'email_events',
                    ['message_id', 'kind', sa.text('id DESC')])

    op.create_table(
        'application_matches',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('message_id', sa.BigInteger(),
                  sa.ForeignKey('email_messages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('application_id', sa.BigInteger(),
                  sa.ForeignKey('applications.id', ondelete='CASCADE'), nullable=True),
        # Which tier fired. The debug view shows this, and it is how a bad
        # matcher is diagnosed rather than guessed at.
        sa.Column('method', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Text(), nullable=True),
        sa.Column('rationale', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text('now()')),
    )
    # Append-only for the same reason as events: matching RE-RUNS as new board
    # rows and jobs appear, so a message that could not be matched in March may
    # match in September. A column on the message would destroy that history;
    # a NULL application_id here records "we looked and found nothing", which
    # is a different and useful fact from never having looked.
    op.create_index('idx_application_matches_latest', 'application_matches',
                    ['message_id', sa.text('id DESC')])
    op.create_index('idx_application_matches_app', 'application_matches', ['application_id'])

    op.create_table(
        'action_items',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('user_id', sa.BigInteger(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('application_id', sa.BigInteger(),
                  sa.ForeignKey('applications.id', ondelete='CASCADE'), nullable=True),
        sa.Column('event_id', sa.BigInteger(),
                  sa.ForeignKey('email_events.id', ondelete='CASCADE'), nullable=True),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('due_at', sa.TIMESTAMP(timezone=True), nullable=True),
        # How it ended, not whether it is open: "open" is the absence of a
        # resolution, derived at read time. Most of these should resolve
        # automatically when a later event supersedes them - an assessment
        # invite closed by "we received your submission" - which is what makes
        # the system no-touch rather than a second inbox to maintain.
        sa.Column('resolved_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('resolution', sa.Text(), nullable=True),
        sa.Column('resolved_by_event_id', sa.BigInteger(),
                  sa.ForeignKey('email_events.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('idx_action_items_open', 'action_items', ['user_id', 'due_at'],
                    postgresql_where=sa.text('resolved_at IS NULL'))


def downgrade() -> None:
    op.drop_table('action_items')
    op.drop_table('application_matches')
    op.drop_table('email_events')
    op.drop_table('applications')
    op.drop_table('email_messages')
