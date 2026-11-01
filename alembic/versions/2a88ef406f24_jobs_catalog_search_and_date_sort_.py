"""jobs: indexes for the admin catalog's search and date sort

The admin catalog (/jobs/catalog) searches `company ILIKE %q% OR title ILIKE
%q% OR url ILIKE %q%` and sorts by `date_posted DESC NULLS LAST, id DESC`.
On production (2026-11-01, 1.49M jobs, warm cache) the search count was a
parallel seq scan, 2.4 s, and the date sort a seq scan and top-N sort of
every row, 1.7 s. Under load the admin page saw 4.6 to 8.9 s and 3.5 s.

A trigram GIN index per searched column turns the OR into a BitmapOr: an OR
is index-backed only when every arm is. pg_trgm is installed by
f3a4b5c6d7e8. The btree matches the sort and its tiebreak, so the page is
read in index order. Sizes, extrapolated from the same indexes built on a 5%
sample of production: company 33 to 51 MB, title 94 to 147 MB, url 181 to
282 MB, date 46 MB.

Each is built CONCURRENTLY under a lock timeout, an INVALID leftover of an
interrupted build dropped first, as bd1e66f153c3 does.

Revision ID: 2a88ef406f24
Revises: 5e1d2c7a9b40
Create Date: 2026-11-01 10:41:44.738862

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2a88ef406f24"
down_revision: str | None = "5e1d2c7a9b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGRAM_COLUMNS = ("company", "title", "url")
_DATE = "idx_jobs_date_posted"


def _drop_if_invalid(name: str) -> None:
    invalid = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND NOT i.indisvalid"
        ),
        {"name": name},
    )
    if invalid.first():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        for column in _TRIGRAM_COLUMNS:
            name = f"idx_jobs_{column}_trgm"
            _drop_if_invalid(name)
            op.create_index(
                name,
                "jobs",
                [column],
                postgresql_using="gin",
                postgresql_ops={column: "gin_trgm_ops"},
                postgresql_concurrently=True,
                if_not_exists=True,
            )
        _drop_if_invalid(_DATE)
        op.create_index(
            _DATE,
            "jobs",
            [sa.literal_column("date_posted DESC NULLS LAST"), sa.literal_column("id DESC")],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        for name in (_DATE, *(f"idx_jobs_{c}_trgm" for c in _TRIGRAM_COLUMNS)):
            op.drop_index(name, table_name="jobs", postgresql_concurrently=True, if_exists=True)
        op.execute("RESET lock_timeout")
