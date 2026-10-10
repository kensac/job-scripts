"""drop the pay columns on jobs once proven empty

Revision ID: 6cbabaaed16a
Revises: 7cff6a3de670
Create Date: 2026-10-10 14:49:38.779588

The contract half of 45ccde44daca. Pay lives in job_comp; readers moved to it
and the comp sweep stopped writing jobs one release before this one, and the
clear_jobs_pay task emptied the columns. Every image that names them must be
gone from the fleet before this applies: dropping a column breaks any
statement that names it.

Nothing here can drop data. ck_jobs_pay_cleared asserts every pay column is
NULL and comp_extracted is false. It is added NOT VALID (no scan) and then
validated, which scans jobs under SHARE UPDATE EXCLUSIVE: reads and writes
continue. If any row still holds pay, VALIDATE raises, nothing is dropped, and
the container does not start. A rerun replaces the guard and validates again.

The drops take ACCESS EXCLUSIVE briefly and rewrite nothing; the space comes
back with a VACUUM FULL of jobs, which is not part of this migration.
Downgrade adds the columns back empty.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '6cbabaaed16a'
down_revision: Union[str, None] = '7cff6a3de670'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GUARD = 'ck_jobs_pay_cleared'
_NULLABLE = {
    'comp_min': sa.BIGINT(),
    'comp_max': sa.BIGINT(),
    'comp_text': sa.TEXT(),
    'comp_period': sa.TEXT(),
    'comp_currency': sa.TEXT(),
    'comp_basis': sa.TEXT(),
    'comp_content_row_id': sa.BIGINT(),
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        op.execute(f"ALTER TABLE jobs DROP CONSTRAINT IF EXISTS {_GUARD}")
        op.execute(
            f"ALTER TABLE jobs ADD CONSTRAINT {_GUARD} CHECK (NOT comp_extracted AND "
            + " AND ".join(f"{column} IS NULL" for column in _NULLABLE)
            + ") NOT VALID"
        )
        op.execute(f"ALTER TABLE jobs VALIDATE CONSTRAINT {_GUARD}")
        op.execute("RESET lock_timeout")
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.drop_constraint(_GUARD, 'jobs', type_='check')
    for column in (*_NULLABLE, 'comp_extracted'):
        op.drop_column('jobs', column)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '60s'")
    for column, type_ in _NULLABLE.items():
        op.add_column('jobs', sa.Column(column, type_, nullable=True))
    op.add_column(
        'jobs',
        sa.Column('comp_extracted', sa.BOOLEAN(), server_default=sa.text('false'), nullable=False),
    )
