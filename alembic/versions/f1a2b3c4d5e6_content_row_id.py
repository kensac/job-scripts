"""job_requirements/job_embeddings: which content row the answer came from

Revision ID: f1a2b3c4d5e6
Revises: b6d3f8a04e17
Create Date: 2026-09-02 13:10:00.000000

content_hash was written on both tables and read by nothing. Both docstrings
said it existed so a re-scraped page could be told from an unchanged one, and
neither sweep ever asked - so a posting that was scraped again kept its
original extraction and its original embedding forever, and similarity search
matched on text the page no longer has.

The hash alone could not have fixed it. Deciding whether to re-read a page
needs a check that runs over the whole corpus every cycle, and hashing requires
the text, which is TOASTed - so a hash-based sweep would detoast 110 MB to
discover that almost nothing changed.

content_row_id is the cheap half: the id of the ai_queries row the answer was
read from. Ids are not TOASTed, so "is there a newer page for this url" is an
index read. The hash stays and becomes the precise half: when the id HAS moved,
the text is fetched and compared, and an identical re-scrape refreshes the id
without paying for a re-extraction. Cheap to detect, exact to decide.

NULL on every existing row, which reads as "we do not know which row this came
from". Those are picked up once by the next sweep and stamped, at the cost of
one re-extraction each. Backfilling instead would mean guessing the row, and a
wrong guess pins a stale answer permanently - the failure this migration exists
to end.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'b6d3f8a04e17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('job_requirements', 'job_embeddings'):
        op.add_column(table, sa.Column('content_row_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    for table in ('job_requirements', 'job_embeddings'):
        op.drop_column(table, 'content_row_id')
