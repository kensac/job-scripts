"""one head after parallel migrations

#264 (message body_html) and #258 (correction actor) both declared
down_revision = c41d7e9a20b8 and merged within the hour. Neither is wrong and
they touch different tables, but alembic cannot choose between two heads:
`upgrade head` fails outright, so nothing migrates and every deploy stops.

This carries no schema of its own. It exists only to rejoin the two lines so
there is one head again.

`test_alembic_has_exactly_one_head` already covers this and its docstring
describes this exact scenario from a previous occurrence. It passes on each PR
in isolation - each branch has one head relative to its own base - and only
fails once both are on main, which is after the merge that causes it. The test
is right; the moment it can catch this is just later than the moment it is
created.

Revision ID: f9f5500ca8bf
Revises: b8e4f1a06c93, c1f4a8b6e930
"""

from collections.abc import Sequence

revision: str = "f9f5500ca8bf"
down_revision: tuple[str, str] = ("b8e4f1a06c93", "c1f4a8b6e930")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
