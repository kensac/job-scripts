"""screen listed occupations out of the Aerospace New Grad board

Revision ID: c3d9a1f5e8b2
Revises: b7e2c4a91f03
Create Date: 2026-10-18 08:30:00.000000

Data only. Names nontechnical_occupations_v1 as the title screen for prompt
552c92c6e2896e1d (board 6, Aerospace New Grad), the one verification reader
with none; the other boards and user 1's filter already skip these titles,
so verification stops reading a posting only every reader skips. Measured
2026-10-18 on every verdict of that prompt: beyond what the volume gate's
occupation_words_v1 skips, it skips 7,973 of 147,069 judged postings (1,433
in the last 7 days) and none of the board's 440 keeps. Added only where the
prompt has no screen, so an admin's choice is left alone.

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d9a1f5e8b2"
down_revision: str | None = "b7e2c4a91f03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROMPT = "552c92c6e2896e1d"
_RECIPE = "nontechnical_occupations_v1"


def _change(where: str, new_value: str) -> None:
    # Recorded in app_config_changes as the admin API's put_config records an
    # edit, so the config history page shows it.
    op.execute(
        f"""
        WITH before AS (
            SELECT value FROM app_config WHERE key = 'title_screens' AND {where}
            FOR UPDATE
        ), changed AS (
            UPDATE app_config c SET value = {new_value}
            FROM before WHERE c.key = 'title_screens'
            RETURNING before.value AS old_value, c.value AS new_value
        )
        INSERT INTO app_config_changes (key, old_value, new_value, old_value_present, actor_user_id)
        SELECT 'title_screens', old_value, new_value, true, u.id
        FROM changed JOIN users u ON u.id = 1
        """
    )


def upgrade() -> None:
    _change(f"NOT value ? '{_PROMPT}'", f"""c.value || '{{"{_PROMPT}": "{_RECIPE}"}}'""")


def downgrade() -> None:
    _change(f"value->>'{_PROMPT}' = '{_RECIPE}'", f"c.value - '{_PROMPT}'")
