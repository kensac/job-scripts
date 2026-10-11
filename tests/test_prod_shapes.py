"""Check measured catalog shapes and live-writer invariants.

The generated corpus covers production value shapes in pull request tests.
Integration tests require a synced copy when their subject is data produced
by a live writer. Generating rows to satisfy those assertions would make the
tests unable to fail. Examples include an unexpected ATS reason and weekly
pay values that earlier fixtures omitted.
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
    assert jobs > 1000, f"only {jobs} jobs - did the sync run?"
    assert verdicts > 1000, f"only {verdicts} verdicts - did the sync run?"


@pytest.mark.integration
def test_created_at_is_stored_in_utc():
    """The column was naive local time for months, which shifted every window
    query by the container's offset. Nothing should be far in the future, and
    recent rows should be recent.

    Real data only: the subject is what the WRITERS stamped. Corpus timestamps are
    generated from a measured age distribution and are past by construction, so the
    corpus cannot disagree with this.
    """
    future = _count("SELECT count(*) FROM ai_queries WHERE created_at > now() + interval '1 hour'")
    assert future == 0, f"{future} verdicts are stamped in the future"


@pytest.mark.integration
def test_comp_is_always_a_yearly_figure():
    """comp_min/comp_max feed `sort=comp`. A weekly wage stored raw or
    multiplied by 2080 made the column meaningless; both shapes were live.

    Real data only: the subject is what the comp extractor wrote. Over the corpus this
    asserts that the profile's measured comp range is plausible, which is a different
    and much weaker claim.
    """
    absurd = db.query(
        """
        SELECT url, comp_min, comp_max, comp_text FROM job_comp
        WHERE comp_min < 5000 OR comp_max > 5000000
        LIMIT 5
        """
    )
    assert not absurd, f"comp outside a plausible yearly range: {absurd}"


@pytest.mark.integration
def test_comp_min_never_exceeds_comp_max():
    """
    Real data only: the corpus swaps an inverted pair when it builds a row, so over
    generated data this asserts the generator, not the extractor.
    """
    inverted = _count(
        "SELECT count(*) FROM job_comp WHERE comp_min IS NOT NULL "
        "AND comp_max IS NOT NULL AND comp_min > comp_max"
    )
    assert inverted == 0


@pytest.mark.integration
def test_content_rows_record_where_the_text_came_from():
    """The ats_text_collapse detector divides by rows tagged 'ats text' or
    'scraped'. If a writer starts emitting an untagged content row again, the
    detector silently goes blind - as it did before.

    Real data only: 'recent content rows are tagged' is a property of the live writers
    over time. The corpus draws reason and created_at from independent marginals, so it
    holds 'content cached' rows with recent timestamps that production does not have.
    """
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


@pytest.mark.corpus
def test_every_board_row_points_at_a_real_job():
    orphans = _count(
        "SELECT count(*) FROM user_jobs uj LEFT JOIN jobs j ON j.id = uj.job_id WHERE j.id IS NULL"
    )
    assert orphans == 0


@pytest.mark.integration
def test_enabled_filters_have_unique_prompt_hashes_per_user():
    """Two enabled filters sharing a prompt_hash made the board's filter gate
    unsatisfiable, so no new job could ever become visible.

    Real data only: the subject is what users' filter edits produced. The corpus gives
    every generated filter a distinct prompt, so a duplicate hash cannot arise.
    """
    dupes = db.query(
        """
        SELECT user_id, prompt_hash, count(*) AS n FROM user_filters
        WHERE enabled GROUP BY user_id, prompt_hash HAVING count(*) > 1
        """
    )
    assert not dupes, f"duplicate enabled prompt_hash would empty the board: {dupes}"


@pytest.mark.corpus
def test_no_verdict_claims_a_check_type_we_do_not_write():
    known = {"closed", "clearance", "custom", "content", "extraction", "comp"}
    # A person's explain run of a shared check, which no board reads.
    known |= {f"explain:{check}" for check in ("closed", "clearance")}
    seen = {
        r["check_type"]
        for r in db.query(
            "SELECT DISTINCT check_type FROM ai_queries "
            "WHERE created_at > now() - interval '7 days' AND check_type IS NOT NULL"
        )
    }
    assert seen <= known, f"unexpected check types in recent data: {seen - known}"


@pytest.mark.corpus
def test_visibility_predicate_runs_against_real_volume():
    """Not a correctness assertion - a smoke test that the board query still
    executes over the real catalog, which is 100x the size of any fixture."""
    from api.board import criteria
    from api.board.visibility import FULL

    user = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
    if user is None:
        pytest.skip("no users in the synced copy")
    settings = db.query_one("SELECT * FROM user_settings WHERE user_id = %s", (user["id"],))
    sql = FULL.format(columns="COUNT(*) AS c", extra="", criteria=criteria.SQL)
    row = db.query_one(
        sql,
        {"uid": user["id"], "bypass_sponsorship": False, **criteria.params(settings)},
    )
    assert row is not None and row["c"] >= 0


@pytest.mark.integration
def test_no_stored_filter_hash_would_move_under_the_current_template():
    """Compare stored filter hashes with current instructions.

    Filter edits recompute prompt_hash. If the template changes, a later edit can
    orphan prior verdicts and trigger a paid run. The generated corpus computes
    hashes with current code, so only stored real rows can expose that drift.
    Model guidance belongs in the response schema, outside the prompt hash.
    """
    from core.filters import build_custom_instructions, compute_prompt_hash

    rows = db.query("SELECT name, prompt, on_ambiguous, prompt_hash FROM user_filters")
    assert rows, "no filters in the synced database"
    moved = [
        r["name"]
        for r in rows
        if compute_prompt_hash(build_custom_instructions(r["prompt"], r["on_ambiguous"]))
        != r["prompt_hash"]
    ]
    assert not moved, f"these filters would fork their verdict history on the next patch: {moved}"
