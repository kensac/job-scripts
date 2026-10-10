"""seed title_screens from the review gate and drop shadow title gates

Revision ID: acbc00862b2b
Revises: 3c0273da3222
Create Date: 2026-10-10 00:54:36.300843

Data only. `title_screens` takes the prompt hashes `filter_review_gate`
enforced a title recipe for, so the screen stays on across the release that
stops reading the old key. The old key and `filter_routing_policy` stay until
the release that drops the review gate tables, because a container still on
the previous image reads them during the roll.

A board title gate in shadow mode judged every candidate anyway; this release
reads a set gate as enforced, so a shadow gate is cleared rather than
enforced. On 2026-10-10 that is board 6 (aero_major_v1), which is not to be
enforced without a decision.

"""

from typing import Sequence, Union

from alembic import op

revision: str = "acbc00862b2b"
down_revision: Union[str, None] = "3c0273da3222"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Runs before the startup seed, which inserts the empty default only
    # where no row exists.
    op.execute(
        """
        INSERT INTO app_config (key, value)
        SELECT 'title_screens', COALESCE(jsonb_object_agg(scope.key, scope.value->'title_recipe')
                                         FILTER (WHERE scope.value ? 'title_recipe'
                                                 AND jsonb_typeof(scope.value->'title_recipe') = 'string'),
                                         '{}'::jsonb)
        FROM app_config c, jsonb_each(c.value->'scopes') AS scope
        WHERE c.key = 'filter_review_gate' AND c.value->>'title_mode' = 'enforce'
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """
    )
    op.execute(
        "UPDATE managed_boards SET title_gate = NULL WHERE title_gate->>'mode' = 'shadow'"
    )


def downgrade() -> None:
    # A cleared shadow gate is not restored: it changed no run's candidates.
    op.execute("DELETE FROM app_config WHERE key = 'title_screens'")
