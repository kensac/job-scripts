"""health.sources: a host whose page fetches start failing is reported."""

from __future__ import annotations

from api import db
from api.health.sources import _detect_sources


def _fetches(host: str, status: str, age_hours: int, n: int = 20) -> None:
    db.execute(
        "INSERT INTO page_fetches (url, status, method, created_at) "
        "SELECT 'https://' || %s || '/job/' || i, %s, 'scraped', "
        "now() - make_interval(hours => %s) FROM generate_series(1, %s) i",
        (host, status, age_hours, n),
    )


def test_a_host_that_started_failing_fires_and_one_that_always_failed_does_not():
    _fetches("broke.test", "passed", 48)
    _fetches("broke.test", "failed", 2)
    _fetches("always.test", "failed", 48)
    _fetches("always.test", "failed", 2)

    hosts = {f["subject"] for f in _detect_sources() if f["kind"] == "extraction_failing"}
    assert hosts == {"broke.test"}
