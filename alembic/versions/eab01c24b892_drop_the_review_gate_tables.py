"""drop the review gate tables

Revision ID: eab01c24b892
Revises: 98ace4b3fc55
Create Date: 2026-10-10 01:07:37.083851

Nothing has written or read these since the title screen became a candidate
predicate (core/screening.py): the skip set is the screen run again, and over
the 14 days before writes stopped it matched every stored decision (42,146
skips, 231,279 reviews, 0 differences; 2026-10-10). Dropping a table returns
its space at once, with no vacuum: 2.5 GB on 2026-10-10.

Every image that names the tables must be gone from the fleet before this
runs. The config rows the previous image read go with them.

The downgrade recreates the tables empty.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "eab01c24b892"
down_revision: Union[str, None] = "98ace4b3fc55"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ACCESS EXCLUSIVE on tables nothing reads; a wait means something still
    # does, and a failed start that retries says so better than a hang.
    op.execute("SET LOCAL lock_timeout = '30s'")
    # Referencing tables first.
    for table in (
        "review_gate_outcomes",
        "review_gate_decisions",
        "review_gate_decision_bodies",
        "review_gate_urls",
        "review_gate_policies",
    ):
        op.drop_table(table)
    op.execute(
        "DELETE FROM app_config WHERE key IN ('filter_review_gate', 'filter_routing_policy')"
    )


def downgrade() -> None:
    op.create_table(
        "review_gate_policies",
        sa.Column(
            "id",
            sa.BIGINT(),
            sa.Identity(
                always=True,
                start=1,
                increment=1,
                minvalue=1,
                maxvalue=9223372036854775807,
                cycle=False,
                cache=1,
            ),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("digest", postgresql.BYTEA(), autoincrement=False, nullable=False),
        sa.Column(
            "policy", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("review_gate_policies_pkey")),
        sa.UniqueConstraint(
            "digest",
            name=op.f("review_gate_policies_digest_key"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_table(
        "review_gate_urls",
        sa.Column(
            "id",
            sa.BIGINT(),
            sa.Identity(
                always=True,
                start=1,
                increment=1,
                minvalue=1,
                maxvalue=9223372036854775807,
                cycle=False,
                cache=1,
            ),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("url", sa.TEXT(), autoincrement=False, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("review_gate_urls_pkey")),
        sa.UniqueConstraint(
            "url",
            name=op.f("review_gate_urls_url_key"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_table(
        "review_gate_decision_bodies",
        sa.Column(
            "id",
            sa.BIGINT(),
            sa.Identity(
                always=True,
                start=1,
                increment=1,
                minvalue=1,
                maxvalue=9223372036854775807,
                cycle=False,
                cache=1,
            ),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("digest", postgresql.BYTEA(), autoincrement=False, nullable=False),
        sa.Column("prompt_hash", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("stage", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("mode", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("action", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("reason", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("profile_id", sa.BIGINT(), autoincrement=False, nullable=True),
        sa.Column("title", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("content_hash", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("policy_id", sa.BIGINT(), autoincrement=False, nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False
        ),
        sa.CheckConstraint(
            "action = ANY (ARRAY['skip'::text, 'review'::text])",
            name=op.f("ck_review_gate_decision_bodies_action"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["review_gate_policies.id"],
            name=op.f("fk_review_gate_decision_bodies_policy"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("review_gate_decision_bodies_pkey")),
        sa.UniqueConstraint(
            "digest",
            name=op.f("review_gate_decision_bodies_digest_key"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_table(
        "review_gate_decisions",
        sa.Column(
            "id",
            sa.BIGINT(),
            sa.Identity(
                always=True,
                start=1,
                increment=1,
                minvalue=1,
                maxvalue=9223372036854775807,
                cycle=False,
                cache=1,
            ),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("task_id", sa.BIGINT(), autoincrement=False, nullable=False),
        sa.Column("job_id", sa.BIGINT(), autoincrement=False, nullable=True),
        sa.Column("user_id", sa.BIGINT(), autoincrement=False, nullable=True),
        sa.Column("filter_id", sa.BIGINT(), autoincrement=False, nullable=True),
        sa.Column("managed_board_id", sa.BIGINT(), autoincrement=False, nullable=True),
        sa.Column("revision", sa.BIGINT(), autoincrement=False, nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("url_id", sa.BIGINT(), autoincrement=False, nullable=False),
        sa.Column("body_id", sa.BIGINT(), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(
            ["body_id"],
            ["review_gate_decision_bodies.id"],
            name=op.f("fk_review_gate_decisions_body"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["url_id"],
            ["review_gate_urls.id"],
            name=op.f("fk_review_gate_decisions_url"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("review_gate_decisions_pkey")),
        postgresql_with={
            "autovacuum_vacuum_scale_factor": "0.02",
            "autovacuum_vacuum_insert_scale_factor": "0.02",
            "autovacuum_analyze_scale_factor": "0.01",
        },
    )
    op.create_index(
        op.f("uq_review_gate_decisions_task_url_id"),
        "review_gate_decisions",
        ["task_id", "url_id"],
        unique=True,
    )
    op.create_index(
        op.f("idx_review_gate_decisions_url_id"), "review_gate_decisions", ["url_id"], unique=False
    )
    op.create_index(
        op.f("idx_review_gate_decisions_created"),
        "review_gate_decisions",
        ["created_at"],
        unique=False,
    )
    op.create_table(
        "review_gate_outcomes",
        sa.Column(
            "id",
            sa.BIGINT(),
            sa.Identity(
                always=True,
                start=1,
                increment=1,
                minvalue=1,
                maxvalue=9223372036854775807,
                cycle=False,
                cache=1,
            ),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("decision_id", sa.BIGINT(), autoincrement=False, nullable=False),
        sa.Column("query_id", sa.BIGINT(), autoincrement=False, nullable=False),
        sa.Column("batch_id", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("model", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("rejected", sa.BOOLEAN(), autoincrement=False, nullable=True),
        sa.Column("outcome", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("recorded_cost_usd", sa.NUMERIC(), autoincrement=False, nullable=True),
        sa.Column(
            "usage", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("review_gate_outcomes_pkey")),
        sa.UniqueConstraint(
            "decision_id",
            "query_id",
            name=op.f("uq_review_gate_outcomes_decision_query"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_index(
        op.f("idx_review_gate_outcomes_decision"),
        "review_gate_outcomes",
        ["decision_id"],
        unique=False,
    )
