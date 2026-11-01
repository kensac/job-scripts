"""Board analytics over a full catalog.

The unit suite proves the arithmetic. These prove the assumptions the metrics
are built on still hold over a realistic population - specifically the two
that invert the obvious reading of the numbers, and the ones that decide which
metrics are honest to render at all.

They run on the generated corpus, and therefore on every pull request.
"""

from __future__ import annotations

import time

import pytest

from api import db
from api.routers import analytics


def _sources() -> list[analytics.BoardAnalytics]:
    return analytics._collect(analytics.DEFAULT_MIN_SAMPLE)


@pytest.mark.corpus
def test_every_source_in_jobs_survives_into_the_response():
    """sheet_import and upload have no row in `sources`. An inner join drops
    them, and sheet_import is the largest board in the catalog."""
    rows = {r.source for r in _sources()}
    in_jobs = {r["source"] for r in db.query("SELECT DISTINCT source FROM jobs")}
    assert in_jobs <= rows, f"sources lost between jobs and the response: {in_jobs - rows}"


@pytest.mark.corpus
def test_unconfigured_sources_are_reported_as_such_not_omitted():
    rows = {r.source: r for r in _sources()}
    configured = {r["name"] for r in db.query("SELECT name FROM sources")}
    for name, row in rows.items():
        assert row.configured is (name in configured)


@pytest.mark.corpus
def test_a_job_belongs_to_exactly_one_source():
    """Why overlap is keyed on (company, title) rather than url."""
    dupes = db.query_one(
        "SELECT count(*) AS c FROM (SELECT url FROM jobs GROUP BY url HAVING count(*) > 1) t"
    )
    assert dupes is not None and dupes["c"] == 0


@pytest.mark.corpus
def test_rates_never_render_a_number_below_the_floor():
    for row in _sources():
        for name, stage in row.funnel.items():
            rate = stage.pass_rate
            if rate.denominator < analytics.DEFAULT_MIN_SAMPLE:
                assert rate.value is None, f"{row.source}/{name} rendered a floored rate"


@pytest.mark.corpus
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
        _check(row.model_dump(), row.source)


@pytest.mark.corpus
def test_funnel_never_claims_to_have_checked_more_than_the_board_holds():
    for row in _sources():
        total = row.inventory.total
        for name, stage in row.funnel.items():
            assert stage.checked <= total, f"{row.source}/{name} checked > total"


@pytest.mark.corpus
def test_collect_stays_within_its_measured_budget():
    """The queries were tuned against production (the funnel alone went from
    ~1.15s to ~245ms by deduping on jobs.id instead of the url text). This
    catches a regression that reintroduces a disk-spilling sort, without
    asserting a wall-clock number tight enough to flake on a loaded machine."""
    start = time.monotonic()
    _sources()
    elapsed = time.monotonic() - start
    assert elapsed < 10.0, f"_collect took {elapsed:.1f}s"
