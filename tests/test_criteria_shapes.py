"""api.board.criteria evaluates the location rule in two shapes: per posting
(CONDITIONS, json_sql, api.posting_path) and once per statement for a scan
(SQL). They must be one rule: the same postings for every criterion."""

from __future__ import annotations

import pytest

from api import db
from api.board import criteria

# text, country, region, city, remote, places
LOCATIONS = [
    ("United States", "US", None, None, False, [("US", None, None)]),
    ("New York, NY", "US", "NY", "New York", False, [("US", "NY", "New York")]),
    ("Austin, TX", "US", "TX", "Austin", False, [("US", "TX", "Austin")]),
    ("Texas", "US", "TX", None, False, [("US", "TX", None)]),
    ("Toronto", "CA", "ON", "Toronto", False, [("CA", "ON", "Toronto")]),
    ("London", "GB", None, "London", False, [("GB", None, "London")]),
    (
        "NYC or London",
        "US",
        "NY",
        "New York",
        False,
        [("US", "NY", "New York"), ("GB", None, "London")],
    ),
    ("Remote", None, None, None, True, []),
    ("Remote US", "US", None, None, True, [("US", None, None)]),
    ("Multiple locations", None, None, None, False, []),
]

# Each posting's location strings. "Atlantis" was never classified, and the
# last posting names no location at all.
POSTINGS = {
    "us": ["United States"],
    "nyc": ["New York, NY"],
    "austin": ["Austin, TX"],
    "toronto": ["Toronto"],
    "london": [" London "],
    "either": ["NYC or London"],
    "remote": ["Remote"],
    "remote_us": ["Remote US"],
    "multi": ["Multiple locations"],
    "unclassified": ["Atlantis"],
    "two": ["Toronto", "Austin, TX"],
    "none": [],
}

# (criterion, which list) -> the postings that pass.
EXPECTED = {
    (("United States",), "excl"): {"toronto", "london", "remote", "multi", "unclassified", "none"},
    (("United States",), "incl"): {"us", "nyc", "austin", "either", "remote_us", "two", "none"},
    (("Texas",), "incl"): {"austin", "two", "none"},
    (("Austin, TX",), "excl"): {
        "us",
        "nyc",
        "toronto",
        "london",
        "either",
        "remote",
        "remote_us",
        "multi",
        "unclassified",
        "none",
    },
    (("Remote",), "incl"): {"remote", "remote_us", "none"},
    (("Remote",), "excl"): {
        "us",
        "nyc",
        "austin",
        "toronto",
        "london",
        "either",
        "multi",
        "unclassified",
        "two",
        "none",
    },
    (("London", "Remote"), "incl"): {"london", "either", "remote", "remote_us", "none"},
    (("Atlantis",), "excl"): set(POSTINGS),
}


def _place(country, region, city):
    return {"country": country, "region": region, "city": city}


@pytest.fixture
def catalog():
    for text, country, region, city, remote, places in LOCATIONS:
        db.execute(
            "INSERT INTO locations (text, country, region, city, remote, places) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (text, country, region, city, remote, db.jsonb([_place(*p) for p in places])),
        )
    ids = {}
    for name, locations in POSTINGS.items():
        row = db.query_one(
            "INSERT INTO jobs (url, raw_url, source, company, title, active, available, "
            "locations) VALUES (%s, %s, 'src', 'Acme', 'Engineer', true, true, %s) "
            "RETURNING id",
            (f"https://jobs.test/{name}", f"https://jobs.test/{name}", locations),
        )
        assert row is not None
        ids[row["id"]] = name
    return ids


def _passing(ids: dict[int, str], where: str, params: dict) -> set[str]:
    rows = db.query(f"SELECT j.id FROM jobs j WHERE TRUE {where}", params)
    return {ids[r["id"]] for r in rows}


@pytest.mark.parametrize(("criterion", "which"), list(EXPECTED))
def test_both_shapes_are_one_rule(catalog, criterion, which):
    key = "excluded_locations" if which == "excl" else "included_locations"
    params = criteria.params({"criteria": {key: list(criterion)}})

    scanned = _passing(catalog, criteria.SQL, params)
    one_by_one = _passing(catalog, criteria.PER_POSTING_SQL, params)

    assert scanned == one_by_one == EXPECTED[(criterion, which)]


def test_stored_criteria_bind_the_per_posting_shape(catalog):
    """json_sql reads a criterion per row, so it must not be the per-statement
    set, which would be rebuilt for every row it is correlated with."""
    stored = criteria.json_sql('\'{"included_locations": ["Texas"]}\'::jsonb')
    assert "IS TRUE" not in stored
    assert _passing(catalog, stored, {}) == EXPECTED[(("Texas",), "incl")]
