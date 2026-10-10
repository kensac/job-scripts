"""drop jobs.uploaded_by

Revision ID: 63f0ea027862
Revises: ed4c4525c227
Create Date: 2026-10-10 19:31:16.075580

The contract step of moving a person's upload off the catalog row
(docs/agents/sources-and-boards.md). posting_uploads holds who uploaded a
posting since #936, every reader moved to it in #941, and nothing has written
jobs.uploaded_by since #966. Its clear_upload_columns task emptied the column:
run 9433478 cleared 11 rows with 0 unmatched (every uploader matched its
posting_uploads row), and run 9446395 found nothing. Production on 2026-10-10
at 23:01 UTC: 0 of 1,035,021 rows hold a value.

jobs.extraction_status is not dropped. It holds the done stamp on 6,021
sheet_import rows and on job 11764337, recorded nowhere else, so it is kept
and frozen: nothing reads or writes it.

Nothing here can drop data. ck_jobs_uploaded_by_empty asserts the column is
NULL. It is added NOT VALID (no scan) and then validated, which scans jobs
under SHARE UPDATE EXCLUSIVE: reads and writes continue. If any row holds a
value, VALIDATE raises, nothing is dropped, and the container does not start.
Until the drop commits, the guard also refuses a new value.

Lock behaviour, in order:
- ADD CONSTRAINT ... NOT VALID: ACCESS EXCLUSIVE, catalog only, under
  lock_timeout.
- VALIDATE: SHARE UPDATE EXCLUSIVE on jobs, one pass over the heap.
- DROP INDEX CONCURRENTLY idx_jobs_uploaded_by: SHARE UPDATE EXCLUSIVE.
- One short transaction: drop the foreign key and the column (the guard goes
  with it). ACCESS EXCLUSIVE briefly, under lock_timeout; nothing is
  rewritten.

The last release that names the column is #941's: #966 names it only in
clear_upload_columns, which this release removes. A worker still on #966
during the roll fails that one background task until it is replaced.
Downgrade adds the column back empty, with its foreign key and index.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "63f0ea027862"
down_revision: Union[str, None] = "ed4c4525c227"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GUARD = "ck_jobs_uploaded_by_empty"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        op.execute(f"ALTER TABLE jobs DROP CONSTRAINT IF EXISTS {_GUARD}")
        op.execute(f"ALTER TABLE jobs ADD CONSTRAINT {_GUARD} CHECK (uploaded_by IS NULL) NOT VALID")
        op.execute(f"ALTER TABLE jobs VALIDATE CONSTRAINT {_GUARD}")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_uploaded_by")
        op.execute("RESET lock_timeout")
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.drop_constraint(op.f("jobs_uploaded_by_fkey"), "jobs", type_="foreignkey")
    op.drop_column("jobs", "uploaded_by")


def downgrade() -> None:
    op.add_column(
        "jobs", sa.Column("uploaded_by", sa.BIGINT(), autoincrement=False, nullable=True)
    )
    op.create_foreign_key(op.f("jobs_uploaded_by_fkey"), "jobs", "users", ["uploaded_by"], ["id"])
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_uploaded_by ON jobs (uploaded_by)")
