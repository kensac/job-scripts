"""host_budget: fold rows kept under a platform's own hosts into its pace key

core.fetching.hosts.pace_key is now the one key a pace is kept under. Rows
written under an older spelling of it are learned state no pull reads again,
and the admin host budgets page lists them forever. Production on 2026-10-10:
629 Workday tenant rows from before the platform fold (last written
2026-09-12, every one at pace 0 with no refusal), 2 job-boards.greenhouse.io
rows from before the form fold (2026-09-06), and 15 Eightfold tenant rows,
live, of which Microsoft and Qualcomm had learned 24.3 s and 30 s gaps.

Each (key, address) takes the widest gap and the latest slot of the rows
folded into it, so no address speeds up against a host that refused it, and
the sum of their counts, so the blocked detector (ok = 0, refused >= 3) reads
the same history. The rules are written out here rather than imported, so the
migration means the same thing after the function changes. 1,395 rows, one
statement.

Revision ID: bc1978f126b4
Revises: ce29f52b5f6b
Create Date: 2026-10-09 23:19:15.238387

"""

from collections.abc import Sequence

from alembic import op

revision: str = "bc1978f126b4"
down_revision: str | None = "ce29f52b5f6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        WITH eightfold AS (
            SELECT lower(substring(listings_url from '//([^/:?#]+)')) AS host FROM sources
             WHERE rtrim(split_part(listings_url, '?', 1), '/')
                   ~ '(/api/pcsx/search|/api/apply/v2/jobs)$'
        ), keyed AS (
            SELECT host, egress_group,
                   CASE
                     WHEN host LIKE '%.myworkdayjobs.com' THEN 'myworkdayjobs.com'
                     WHEN host LIKE '%.greenhouse.io' AND host <> 'boards-api.greenhouse.io'
                       THEN 'boards-api.greenhouse.io'
                     WHEN host LIKE '%.eightfold.ai' OR host IN (SELECT host FROM eightfold)
                       THEN 'eightfold.ai'
                   END AS key
              FROM host_budget
        ), moved AS (
            DELETE FROM host_budget b USING keyed k
             WHERE b.host = k.host AND b.egress_group = k.egress_group AND k.key IS NOT NULL
            RETURNING k.key, b.egress_group, b.pace_seconds, b.next_allowed_at, b.ok,
                      b.refused, b.updated_at
        )
        INSERT INTO host_budget
            (host, egress_group, pace_seconds, next_allowed_at, ok, refused, updated_at)
        SELECT key, egress_group, max(pace_seconds), max(next_allowed_at), sum(ok),
               sum(refused), max(updated_at)
          FROM moved GROUP BY key, egress_group
        ON CONFLICT (host, egress_group) DO UPDATE SET
            pace_seconds = GREATEST(host_budget.pace_seconds, EXCLUDED.pace_seconds),
            next_allowed_at = GREATEST(host_budget.next_allowed_at, EXCLUDED.next_allowed_at),
            ok = host_budget.ok + EXCLUDED.ok,
            refused = host_budget.refused + EXCLUDED.refused,
            updated_at = GREATEST(host_budget.updated_at, EXCLUDED.updated_at)
        """
    )


def downgrade() -> None:
    # The folded rows cannot be split back into their hosts, and the older
    # code relearns a row it does not find from its first pull.
    pass
