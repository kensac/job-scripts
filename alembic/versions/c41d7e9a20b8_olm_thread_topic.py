"""olm's provider_thread_id is a subject, not a thread

Outlook .olm exports carry OPFMessageCopyThreadTopic, which is a normalised
SUBJECT. The importer stored it in provider_thread_id, so a column named for a
provider-issued conversation id held a string that collides across every
employer sharing a subject line.

That is not a cosmetic mislabel. seed_from_mail groups by thread in preference
to (company, title), so every ATS autoresponder with a common subject
collapsed into ONE derived application, taking company and title from whichever
message happened to sort first. "Nittany Lion Careers Application Confirmation"
is 56 messages spanning 32 distinct employers.

The value is moved rather than dropped: it is a real signal for grouping mail
WITHIN one employer, and Outlook's own threading uses it. It just is not a
thread identity, so it does not get to sit in the column that is.

Scoped to source = 'olm' rather than to `provider_thread_id = subject`. The
latter identifies most of them - 26,131 of 28,451 - but not all: 926 olm thread
groups differ from their subject after normalisation, and 19 of those still
span more than one employer. Every olm row got this column from ThreadTopic, so
the source is the exact rule and the string comparison is only a symptom of it.

takeout and gmail are untouched. Their provider_thread_id is a real threading
identity - the first References entry, and Gmail's own threadId - and neither
has ever equalled its subject.

Revision ID: c41d7e9a20b8
Revises: a7f3c9e1d582
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c41d7e9a20b8"
down_revision: str | None = "a7f3c9e1d582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("thread_topic", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE email_messages
        SET thread_topic = provider_thread_id, provider_thread_id = NULL
        WHERE source = 'olm' AND provider_thread_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE email_messages
        SET provider_thread_id = thread_topic
        WHERE source = 'olm' AND thread_topic IS NOT NULL
        """
    )
    op.drop_column("email_messages", "thread_topic")
