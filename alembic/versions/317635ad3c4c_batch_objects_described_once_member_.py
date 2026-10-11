"""batch objects described once; member rows point at them

Every batch_requests row repeated its bundle's bucket, key, sha256, size and
version inside snapshot_ref: 1,613,017 rows named 6,392 objects, and the
copies were 378 MB of the column's 703 MB (production, 2026-10-11). Each
object now has one row in batch_objects and batch_requests.object_id points
at it. The data moves in tasks.batch_objects, not here (migrations.md).

worker_status.data_level and data_level_release are how that task knows no
image older than this one is still running before it removes the copies.

Autovacuum is set for the backfill first: the task rewrites every row twice
(the pointer, then the removal). At the stock scale factor of 0.2 the table
holds about 320k dead versions before vacuum frees any space; at 0.02 about
32k, the values 40e76006134a chose for listings. ALTER TABLE SET takes SHARE
UPDATE EXCLUSIVE and rewrites nothing.

The foreign key is NOT VALID: adding it validated would scan the table under
SHARE ROW EXCLUSIVE, blocking every batch submission for the scan. It still
checks every row written from here on, and every existing row is NULL.

Revision ID: 317635ad3c4c
Revises: a0e5b60bf58d
Create Date: 2026-10-11 01:56:47.703737

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "317635ad3c4c"
down_revision: str | None = "a0e5b60bf58d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTOVACUUM = {
    "autovacuum_vacuum_scale_factor": "0.02",
    "autovacuum_analyze_scale_factor": "0.01",
}


def upgrade() -> None:
    op.create_table(
        "batch_objects",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket", "key"),
    )
    # Brief locks on tables every batch submission and every worker writes;
    # a minute is bd1e66f153c3's judgment of what a start can afford.
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.add_column("batch_requests", sa.Column("object_id", sa.BigInteger(), nullable=True))
    op.execute(
        "ALTER TABLE batch_requests ADD CONSTRAINT fk_batch_requests_object "
        "FOREIGN KEY (object_id) REFERENCES batch_objects (id) NOT VALID"
    )
    op.execute(
        "ALTER TABLE batch_requests SET ("
        + ", ".join(f"{k} = {v}" for k, v in _AUTOVACUUM.items())
        + ")"
    )
    op.add_column("worker_status", sa.Column("data_level", sa.Integer(), nullable=True))
    op.add_column("worker_status", sa.Column("data_level_release", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("worker_status", "data_level_release")
    op.drop_column("worker_status", "data_level")
    op.execute(f"ALTER TABLE batch_requests RESET ({', '.join(_AUTOVACUUM)})")
    op.drop_constraint("fk_batch_requests_object", "batch_requests", type_="foreignkey")
    op.drop_column("batch_requests", "object_id")
    op.drop_table("batch_objects")
