"""company_text_profiles: the rule each profile was built under

Revision ID: f6b2d8e14a37
Revises: e3f1a7c95b20
Create Date: 2026-11-26

A profile is rebuilt when its company gains titles, and now also when it was
built under an older rule than core.derivations.company_text.PROFILE_RULE.
Rule 1 stripped every paragraph repeated under three titles, which took role
requirements with it; the profiles built under it before local embedding was
switched off are rebuilt under rule 2. Existing rows are rule 1.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6b2d8e14a37"
down_revision: str | None = "e3f1a7c95b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "company_text_profiles",
        sa.Column("rule", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("company_text_profiles", "rule")
