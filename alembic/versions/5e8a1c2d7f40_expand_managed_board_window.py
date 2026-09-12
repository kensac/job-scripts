"""expand managed public board posting window

Revision ID: 5e8a1c2d7f40
Revises: 39476e971cb3
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5e8a1c2d7f40"
down_revision: str | Sequence[str] | None = "39476e971cb3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SLUGS = ("software-engineering-internships", "software-engineering-new-grad")


def _set_window(days: int, expected_days: int) -> None:
    op.execute(
        f"""
        UPDATE managed_boards
        SET criteria = jsonb_set(criteria, '{{max_age_days}}', to_jsonb({days})),
            revision = revision + 1,
            updated_at = now()
        WHERE slug IN {_SLUGS!r}
          AND (criteria->>'max_age_days')::integer = {expected_days}
        """
    )


def upgrade() -> None:
    _set_window(30, 7)


def downgrade() -> None:
    _set_window(7, 30)
