"""Integration tests against a synced copy of production.

These exist because unit tests assert what we believe the data looks like,
which is exactly the assumption that has been wrong most often here: the
ats_text_collapse detector was blind for its whole life because real rows had
a `reason` value no fixture ever produced, and the comp column was unsortable
because real postings advertise weekly pay that no test ever wrote.

Run with:
    set -a && . ./.env && set +a
    make testdb-sync
    TEST_DATABASE_URL=...jobtracker_test make integration

They are read-mostly and assert invariants that must hold over real data
rather than exact values, so they do not break every time the catalog moves.
"""

from __future__ import annotations

import pytest

from api import db

pytestmark = pytest.mark.integration


def _count(sql: str, params=None) -> int:
    row = db.query_one(sql, params)
    assert row is not None
    return int(next(iter(row.values())))


def test_synced_database_actually_has_production_data():
    """Guards every other test in this file: if the sync silently produced an
    empty database, the rest would pass vacuously."""
    jobs = _count("SELECT count(*) FROM jobs")
    verdicts = _count("SELECT count(*) FROM ai_queries")
    assert jobs > 1000, f"only {jobs} jobs - did the sync run?"
    assert verdicts > 1000, f"only {verdicts} verdicts - did the sync run?"


def test_created_at_is_stored_in_utc():
    """The column was naive local time for months, which shifted every window
    query by the container's offset. Nothing should be far in the future, and
    recent rows should be recent."""
    future = _count("SELECT count(*) FROM ai_queries WHERE created_at > now() + interval '1 hour'")
    assert future == 0, f"{future} verdicts are stamped in the future"


def test_comp_is_always_a_yearly_figure():
    """comp_min/comp_max feed `sort=comp`. A weekly wage stored raw or
    multiplied by 2080 made the column meaningless; both shapes were live."""
    absurd = db.query(
        """
        SELECT url, comp_min, comp_max, comp_text FROM jobs
        WHERE comp_extracted AND (comp_min < 5000 OR comp_max > 5000000)
        LIMIT 5
        """
    )
    assert not absurd, f"comp outside a plausible yearly range: {absurd}"


def test_comp_min_never_exceeds_comp_max():
    inverted = _count(
        "SELECT count(*) FROM jobs WHERE comp_min IS NOT NULL "
        "AND comp_max IS NOT NULL AND comp_min > comp_max"
    )
    assert inverted == 0


def test_content_rows_record_where_the_text_came_from():
    """The ats_text_collapse detector divides by rows tagged 'ats text' or
    'scraped'. If a writer starts emitting an untagged content row again, the
    detector silently goes blind - as it did before."""
    recent_untagged = _count(
        """
        SELECT count(*) FROM ai_queries
        WHERE check_type = 'content'
          AND created_at > now() - interval '2 days'
          AND reason NOT IN ('ats text', 'scraped')
        """
    )
    assert recent_untagged == 0, (
        f"{recent_untagged} recent content rows carry no origin; the ATS "
        "collapse detector cannot see past them"
    )


def test_every_board_row_points_at_a_real_job():
    orphans = _count(
        "SELECT count(*) FROM user_jobs uj LEFT JOIN jobs j ON j.id = uj.job_id WHERE j.id IS NULL"
    )
    assert orphans == 0


def test_enabled_filters_have_unique_prompt_hashes_per_user():
    """Two enabled filters sharing a prompt_hash made the board's filter gate
    unsatisfiable, so no new job could ever become visible."""
    dupes = db.query(
        """
        SELECT user_id, prompt_hash, count(*) AS n FROM user_filters
        WHERE enabled GROUP BY user_id, prompt_hash HAVING count(*) > 1
        """
    )
    assert not dupes, f"duplicate enabled prompt_hash would empty the board: {dupes}"


def test_no_verdict_claims_a_check_type_we_do_not_write():
    known = {"closed", "clearance", "custom", "content", "extraction", "comp"}
    seen = {
        r["check_type"]
        for r in db.query(
            "SELECT DISTINCT check_type FROM ai_queries "
            "WHERE created_at > now() - interval '7 days' AND check_type IS NOT NULL"
        )
    }
    assert seen <= known, f"unexpected check types in recent data: {seen - known}"


def test_visibility_predicate_runs_against_real_volume():
    """Not a correctness assertion - a smoke test that the board query still
    executes over the real catalog, which is 100x the size of any fixture."""
    from api import criteria
    from api.routers.jobs import _VISIBILITY

    user = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
    if user is None:
        pytest.skip("no users in the synced copy")
    settings = db.query_one("SELECT * FROM user_settings WHERE user_id = %s", (user["id"],))
    sql = _VISIBILITY.format(columns="COUNT(*) AS c", extra="", criteria=criteria.SQL)
    row = db.query_one(
        sql,
        {"uid": user["id"], "bypass_sponsorship": False, **criteria.params(settings)},
    )
    assert row is not None and row["c"] >= 0
