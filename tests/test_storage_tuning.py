"""Catalog state that autogenerate could re-add.

The dropped indexes are checked against the migrated database rather than
the models, which already lack them, so a migration that forgot a drop fails
here.
"""

from api import db


def test_dropped_indexes_are_gone_and_the_url_reads_keep_theirs():
    present = {
        row["indexname"]
        for row in db.query("SELECT indexname FROM pg_indexes WHERE tablename = 'ai_queries'")
    }
    assert not present & {
        "idx_ai_queries_job_title_trgm",
        "idx_ai_queries_company_trgm",
        "idx_ai_queries_cost_created",
    }
    assert {
        "idx_ai_queries_url_trgm",
        "idx_ai_queries_reason_trgm",
    } <= present
