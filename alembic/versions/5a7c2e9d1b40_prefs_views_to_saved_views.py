"""prefs.views to saved_views

Board views live only in saved_views. This revision once converted every page
still holding user_settings.prefs.views into saved_views rows. Every database
that held the old shape has applied it (production on 2026-10-10: 1 view on 1
page, already copied, so it converted nothing), and the frontend no longer
reads prefs.views, so the conversion was removed and the revision is kept only
as a link in the chain.

Revision ID: 5a7c2e9d1b40
Revises: 40e76006134a
Create Date: 2026-10-10 01:00:00.000000

"""
from typing import Sequence, Union

revision: str = '5a7c2e9d1b40'
down_revision: Union[str, None] = '40e76006134a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
