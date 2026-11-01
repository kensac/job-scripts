"""Check measured catalog shapes over the generated corpus.

Whether production itself still holds an invariant (no wedged task, comp
stored yearly) is not a test: generated rows cannot make it fail. That is the
health detectors' job, against live data (api.health).
"""

from __future__ import annotations

import pytest

from api import db


def _count(sql: str, params=None) -> int:
    row = db.query_one(sql, params)
    assert row is not None
    return int(next(iter(row.values())))


@pytest.mark.corpus
def test_the_catalog_is_big_enough_to_assert_anything_over():
    """Guards every other corpus test here: if the build silently produced an
    empty database, the rest would pass vacuously. The floors are the ones the
    analytics assertions need a denominator for."""
    jobs = _count("SELECT count(*) FROM jobs")
    verdicts = _count("SELECT count(*) FROM ai_queries")
    assert jobs > 1000, f"only {jobs} jobs - did the corpus build?"
    assert verdicts > 1000, f"only {verdicts} verdicts - did the corpus build?"


@pytest.mark.corpus
def test_visibility_predicate_runs_against_real_volume():
    """Not a correctness assertion - a smoke test that the board query still
    executes over the real catalog, which is 100x the size of any fixture."""
    from api.board import criteria
    from api.board.visibility import FULL

    user = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
    assert user is not None, "the corpus holds five users"
    settings = db.query_one("SELECT * FROM user_settings WHERE user_id = %s", (user["id"],))
    sql = FULL.format(columns="COUNT(*) AS c", extra="", criteria=criteria.SQL)
    row = db.query_one(
        sql,
        {"uid": user["id"], "bypass_sponsorship": False, **criteria.params(settings)},
    )
    assert row is not None and row["c"] >= 0
