"""Catalog state that autogenerate cannot see, or that it could re-add.

Per-table reloptions are invisible to the drift check, so only a test notices
a migration that stopped setting them. The dropped indexes are checked
against the migrated database rather than the models, which already lack
them, so a migration that forgot a drop fails here too.
"""

from api import db


def test_review_gate_decisions_vacuums_before_a_backfill_extends_the_file():
    row = db.query_one(
        "SELECT reloptions FROM pg_class WHERE oid = 'review_gate_decisions'::regclass"
    )
    assert row is not None
    assert set(row["reloptions"] or []) >= {
        "autovacuum_vacuum_scale_factor=0.02",
        "autovacuum_vacuum_insert_scale_factor=0.02",
        "autovacuum_analyze_scale_factor=0.01",
    }


def test_unscanned_indexes_are_gone_and_the_url_read_keeps_its_index():
    present = {
        row["indexname"]
        for row in db.query(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename IN ('ai_queries', 'review_gate_decisions')"
        )
    }
    assert not present & {
        "idx_ai_queries_job_title_trgm",
        "idx_ai_queries_company_trgm",
        "idx_ai_queries_cost_created",
        "idx_review_gate_decisions_user_created",
    }
    assert {
        "idx_review_gate_decisions_url_created",
        "idx_ai_queries_url_trgm",
        "idx_ai_queries_reason_trgm",
    } <= present
