"""job_embeddings: one vector per posting, and the vector extension

Revision ID: b7c8d9e0f1a2
Revises: b7e4f1a90c23
Create Date: 2026-09-02 01:05:00.000000

Companion to job_requirements. That table answers "what does this posting
say"; this one answers "what else reads like it", which is the question
behind more-roles-like-this-one, cross-board duplicate detection and any
clustering of the corpus.

Separate table rather than a column on job_requirements, for two reasons. The
rows are not the same rows - a page can embed cleanly and still yield no
stated requirements, and the two sweeps fail independently - and, concretely,
routers/requirements.py's slice does SELECT DISTINCT r.*, which would drag a
6 KB vector through a DISTINCT on every market and gap request.

Keyed by url and with no foreign key to jobs, for the reason job_requirements
is: a quarter of the urls with stored page text have no job row, and those
postings are the ones that can never be re-scraped.

DELIBERATELY NO VECTOR INDEX. Measured on this image at full corpus size -
20,730 rows of vector(1536), which is a 166 MB table:

    unfiltered top-10 over the whole corpus, no index    154-256 ms
    the same, with an HNSW index                         0.31-0.48 ms
    HNSW index size                                      101 MB
    top-10 within one user's visible slice (1,703 rows)  9-19 ms, no index

The last line is the one that decides it. Nothing asks "which of all 20,730
postings is most like this"; the question is always "which of the roles I can
see", which is a per-user slice under a tenth of the corpus, and an exact scan
answers that in single-digit milliseconds. Buying 500x on a query nobody
issues would cost 101 MB - 61% of the table's own size - and would make the
query that IS issued worse, not better: pgvector's HNSW under a selective
filter either post-filters and returns fewer than k rows or falls back to an
iterative scan, so the index trades exactness for speed the filtered query
does not need.

Revisit if an unfiltered whole-corpus query ever appears - global duplicate
detection across every board would be one - and revisit on row count, since
the exact scan is linear and this reasoning is not.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'b7e4f1a90c23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen literal, not an import from core.embeddings. A migration has to mean
# the same thing forever, and a constant that later moved would silently
# rewrite what this one did. core.embeddings.EMBEDDING_DIMENSIONS is the live
# value, and a test asserts the created column still matches it.
#
# 1536 is text-embedding-3-small's native width, kept rather than reduced.
# Measured over 400 real postings against the full-width neighbour set,
# truncating to 512 dimensions retained 82% of the top-10 neighbours and 256
# retained 70%. Losing one recommendation in five to save 85 MB on a database
# of this size is the wrong side of that trade; at ten million rows it is not.
_EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    # Present on prod (0.8.6) and on the image CI and `make testdb-up` use.
    # A local Postgres without it fails here, and tests/conftest.py catches
    # that specific failure to name pgvector and the way out.
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table(
        'job_embeddings',
        sa.Column('url', sa.Text(), primary_key=True),
        sa.Column('embedding', Vector(_EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column('model', sa.Text(), nullable=False),
        # The page text this vector was computed from, so a re-scraped page can
        # be told from an unchanged one without re-embedding to find out.
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        # Priced by core.pricing at call time, like every other AI spend: the
        # rate changes, so deriving it on read would rewrite history. NULL
        # stays distinct from zero - "no published price" is not "free".
        #
        # NUMERIC(14, 10), not the (12, 6) that ai_queries and api_usage use.
        # Those hold the cost of a whole reasoning call, which is cents; one
        # embedding is $0.0000226, and six decimal places rounds that to
        # 0.000023 - a 1.6% overstatement that does not cancel, because it is a
        # rounding in one direction applied 20,730 times. It puts the corpus at
        # $0.4768 against a true $0.4693. Ten places holds the real figure.
        sa.Column('cost_usd', sa.Numeric(14, 10), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )


def downgrade() -> None:
    op.drop_table('job_embeddings')
    # The extension is deliberately left in place: another table may have come
    # to depend on it, and dropping a shared extension to undo one table is a
    # far larger action than this migration took.
