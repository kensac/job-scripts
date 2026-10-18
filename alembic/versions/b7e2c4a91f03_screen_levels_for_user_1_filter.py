"""screen internships and senior-only titles out of user 1's filter

Revision ID: b7e2c4a91f03
Revises: d4428913f75e
Create Date: 2026-10-18 07:50:00.000000

Data only. Names levels_occupations_v1 (core/screening.py, #1133) as the
title screen for prompt 08166655c8627fea, user 1's pay_tier_200_resume_match,
in place of nontechnical_occupations_v1. Measured on every verdict of that
prompt: skips 15.7% (18.2% of the last week), and a paired re-run of the
filter on 302 postings it skips kept none. Only a row still holding the old
recipe changes, so an admin's later edit is left alone. Merged after every
worker ran #1133: a worker that does not know the recipe name rejects the
config value.

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7e2c4a91f03"
down_revision: str | None = "d4428913f75e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROMPT = "08166655c8627fea"


def _swap(old: str, new: str) -> None:
    # Recorded in app_config_changes as the admin API's put_config records an
    # edit, so the config history page shows it; the actor is user 1, whose
    # filter it is.
    op.execute(
        f"""
        WITH before AS (
            SELECT value FROM app_config
            WHERE key = 'title_screens' AND value->>'{_PROMPT}' = '{old}'
            FOR UPDATE
        ), changed AS (
            UPDATE app_config c
            SET value = jsonb_set(c.value, '{{{_PROMPT}}}', '"{new}"')
            FROM before WHERE c.key = 'title_screens'
            RETURNING before.value AS old_value, c.value AS new_value
        )
        INSERT INTO app_config_changes (key, old_value, new_value, old_value_present, actor_user_id)
        SELECT 'title_screens', old_value, new_value, true, u.id
        FROM changed JOIN users u ON u.id = 1
        """
    )


def upgrade() -> None:
    _swap("nontechnical_occupations_v1", "levels_occupations_v1")


def downgrade() -> None:
    _swap("levels_occupations_v1", "nontechnical_occupations_v1")
