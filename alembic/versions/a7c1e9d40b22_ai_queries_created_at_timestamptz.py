"""ai_queries.created_at TEXT -> timestamptz

Revision ID: a7c1e9d40b22
Revises: f2a3b4c5d6e7
Create Date: 2026-09-01 05:40:00.000000

The column held naive LOCAL time (datetime.now().isoformat()) while every
query compared it against Postgres now(), which is UTC. Containers run
TZ=America/New_York, so every time window was shifted by the Eastern offset.

Historical rows are all interpreted as America/New_York: the fleet containers
carry that TZ, and the pre-fleet rows were written by the sheet-era CLI on an
Eastern machine (one is stamped Kanishks-MacBook-Pro.local). AT TIME ZONE with
a named zone resolves each row's own DST offset, so the June-September range
converts correctly without assuming a fixed -4h.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a7c1e9d40b22'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ai_queries is owned by core/store.init_db(), not the ORM, and the two run
    # in whichever order the process imports them. So this has to tolerate the
    # table not existing yet (init_db then creates it timestamptz outright) and
    # the column already being converted (converting twice would shift the
    # values a second time).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'ai_queries' AND column_name = 'created_at'
                  AND data_type = 'text'
            ) THEN
                DROP INDEX IF EXISTS idx_ai_queries_created_at;
                ALTER TABLE ai_queries ALTER COLUMN created_at TYPE timestamptz
                    USING created_at::timestamp AT TIME ZONE 'America/New_York';
                ALTER TABLE ai_queries ALTER COLUMN created_at SET DEFAULT now();
                CREATE INDEX idx_ai_queries_created_at ON ai_queries (created_at);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'ai_queries' AND column_name = 'created_at'
                  AND data_type <> 'text'
            ) THEN
                DROP INDEX IF EXISTS idx_ai_queries_created_at;
                ALTER TABLE ai_queries ALTER COLUMN created_at DROP DEFAULT;
                ALTER TABLE ai_queries ALTER COLUMN created_at TYPE text
                    USING to_char(created_at AT TIME ZONE 'America/New_York',
                                  'YYYY-MM-DD"T"HH24:MI:SS.US');
                CREATE INDEX idx_ai_queries_created_at ON ai_queries (created_at);
            END IF;
        END $$;
        """
    )
