"""merge model_calls backfill columns and dropped prompt tables

#886 (53da75ac7394) and #882 (72dc0fd47e51) both revise 5a7c2e9d1b40, so
main had two heads and `upgrade head` refused to run. They touch different
objects: 53da75ac7394 adds columns to model_calls; 72dc0fd47e51 drops
ai_prompts, ai_prompt_samples, ai_batches.prompt_id and two managed_board_jobs
columns. Nothing to reconcile, so this merge revision has no operations.

Revision ID: 3c0273da3222
Revises: 53da75ac7394, 72dc0fd47e51
Create Date: 2026-10-10 01:40:00.000000

"""

from collections.abc import Sequence

revision: str = "3c0273da3222"
down_revision: str | tuple[str, ...] | None = ("53da75ac7394", "72dc0fd47e51")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
