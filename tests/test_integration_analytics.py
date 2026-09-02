"""Board analytics against a synced copy of production.

The unit suite proves the arithmetic. These prove the assumptions the metrics
are built on still hold over real rows - specifically the two that invert the
obvious reading of the numbers, and the ones that decide which metrics are
honest to render at all.

Run with:
    set -a && . ./.env && set +a
    make testdb-sync
    TEST_DATABASE_URL=...jobtracker_test make integration
"""

from __future__ import annotations

import time

import pytest

from api import db
from api.routers import analytics

pytestmark = pytest.mark.integration

# The spend metric reads ai_queries.cost_usd, which a fixture synced before that
# migration does not have. Skipping loudly beats failing seven tests with an
# UndefinedColumn that says nothing about what to do, and beats dropping the
# column from the query to make a stale fixture pass.
_COST_COLUMN = db.query_one(
    "SELECT 1 AS present FROM information_schema.columns "
    "WHERE table_name = 'ai_queries' AND column_name = 'cost_usd'"
)
if not _COST_COLUMN:
    pytest.skip(
        "synced fixture predates ai_queries.cost_usd - re-run `make testdb-sync`",
        allow_module_level=True,
    )


def _sources() -> list[dict]:
    return analytics._collect(analytics.DEFAULT_MIN_SAMPLE)


def test_every_source_in_jobs_survives_into_the_response():
    """sheet_import and upload have no row in `sources`. An inner join drops
    them, and sheet_import is the largest board in the catalog."""
    rows = {r["source"] for r in _sources()}
    in_jobs = {r["source"] for r in db.query("SELECT DISTINCT source FROM jobs")}
    assert in_jobs <= rows, f"sources lost between jobs and the response: {in_jobs - rows}"


def test_unconfigured_sources_are_reported_as_such_not_omitted():
    rows = {r["source"]: r for r in _sources()}
    configured = {r["name"] for r in db.query("SELECT name FROM sources")}
    for name, row in rows.items():
        assert row["configured"] is (name in configured)


def test_active_share_is_not_comparable_across_feeds():
    """The finding the caveat rests on: some boards have zero inactive rows,
    because their feed only ever lists live postings and the ingest never
    clears the flag. If that ever stops being true the caveat is stale and
    should be revisited rather than left in the response."""
    rows = _sources()
    with_postings = [r for r in rows if r["inventory"]["total"] > 0]
    reporting = [r for r in with_postings if r["inventory"]["reports_inactive"]]
    silent = [r for r in with_postings if not r["inventory"]["reports_inactive"]]
    assert reporting, "no board reports inactive postings - the flag is meaningless"
    assert silent, (
        "every board now reports inactive postings; active_share may have "
        "become comparable and the caveat needs re-deriving"
    )


def test_a_job_belongs_to_exactly_one_source():
    """Why overlap is keyed on (company, title) rather than url."""
    dupes = db.query_one(
        "SELECT count(*) AS c FROM (SELECT url FROM jobs GROUP BY url HAVING count(*) > 1) t"
    )
    assert dupes is not None and dupes["c"] == 0


def test_status_vocabulary_still_has_no_interview_or_offer_state():
    """Interview and offer rates per board are not computable, and this is the
    reason. If a status like that ever appears, the metric becomes available
    and this test is the prompt to add it."""
    statuses = {
        (r["status"] or "").lower()
        for r in db.query("SELECT DISTINCT status FROM user_jobs WHERE status IS NOT NULL")
    }
    outcome_words = {"interview", "offer", "onsite", "phone screen", "rejected"}
    found = {s for s in statuses if any(w in s for w in outcome_words)}
    assert not found, f"outcome statuses now exist ({found}); board outcome rates are computable"


def test_rates_never_render_a_number_below_the_floor():
    for row in _sources():
        for name, stage in row["funnel"].items():
            rate = stage["pass_rate"]
            if rate["denominator"] < analytics.DEFAULT_MIN_SAMPLE:
                assert rate["value"] is None, f"{row['source']}/{name} rendered a floored rate"


def test_every_rate_carries_its_denominator():
    def _check(node, path: str) -> None:
        if isinstance(node, dict):
            if {"value", "numerator", "denominator"} <= node.keys():
                assert isinstance(node["denominator"], int), path
                assert isinstance(node["numerator"], int), path
                assert node["numerator"] <= node["denominator"], path
            for key, value in node.items():
                _check(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _check(value, f"{path}[{i}]")

    for row in _sources():
        _check(row, row["source"])


def test_funnel_never_claims_to_have_checked_more_than_the_board_holds():
    for row in _sources():
        total = row["inventory"]["total"]
        for name, stage in row["funnel"].items():
            assert stage["checked"] <= total, f"{row['source']}/{name} checked > total"


def test_collect_stays_within_its_measured_budget():
    """The queries were tuned against production (the funnel alone went from
    ~1.15s to ~245ms by deduping on jobs.id instead of the url text). This
    catches a regression that reintroduces a disk-spilling sort, without
    asserting a wall-clock number tight enough to flake on a loaded machine."""
    start = time.monotonic()
    _sources()
    elapsed = time.monotonic() - start
    assert elapsed < 10.0, f"_collect took {elapsed:.1f}s"
