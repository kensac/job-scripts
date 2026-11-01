"""Check the visibility projection and the health detectors over the corpus.

Whether the live fleet has a stalled task or batch is the health detectors'
job (api.health), not a test's.

The original test rationale recorded 14,196 verdicts with no matching job and
10,342 active jobs awaiting a first closed verdict on 2026-09-01. Those were
backlogs, not zero-count invariants. The counts are historical and are not
asserted by this module.
"""

from __future__ import annotations

import pytest

from api import db


def _count(sql: str, params=None) -> int:
    row = db.query_one(sql, params)
    assert row is not None
    return int(next(iter(row.values())))


@pytest.mark.corpus
def test_visibility_projection_matches_its_canonical_builder():
    """The materialized visibility projection stores the canonical answer."""
    from api.board import visibility

    user = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
    assert user is not None, "the corpus holds five users"
    uid = user["id"]
    expected = set(visibility.member_ids(uid))
    assert visibility.recompute(uid) == len(expected)
    stored = {
        row["job_id"]
        for row in db.query(
            "SELECT job_id FROM board_visible WHERE user_id = %s ORDER BY job_id", (uid,)
        )
    }
    assert stored == expected


def test_person_touched_history_is_not_materialized_membership(f):
    uid = f.make_user()
    job_id = f.make_job()
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, person_touched_at, notes) "
        "VALUES (%s, %s, now(), 'person note')",
        (uid, job_id),
    )

    assert _count("SELECT count(*) FROM user_job_working_set WHERE user_id = %s", (uid,)) == 0


@pytest.mark.corpus
def test_health_detectors_execute_against_real_data():
    """detect() is ratio comparisons over live tables with sample floors. It
    has silently stopped being evaluable before (a denominator polluted by rows
    no fixture produced), so running it against real volume is the only way to
    know it still works."""
    from api import health

    alerts = health.detect()
    assert isinstance(alerts, list)
    for a in alerts:
        assert {"kind", "subject", "severity", "message"} <= set(a), f"malformed alert: {a}"
