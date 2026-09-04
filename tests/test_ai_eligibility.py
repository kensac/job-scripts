"""Which jobs the paid AI sweeps may touch.

The sweeps that spend tokens used to select postings with no reference to who
subscribes to what, so a job was scraped only if someone wanted its source and
then checked forever regardless. These tests supply postings the gate is
expected to REJECT, so a gate that stops filtering produces rows and fails.
"""

from __future__ import annotations

import pytest

from api import db
from core.store import AI_ELIGIBLE_JOB, SUBSCRIBED_SOURCE

CATALOGUED = "gate-board"
SWITCHED_OFF = "gate-board-off"
UNWANTED = "gate-board-unwanted"
# Not in `sources`: what a person's own import or upload looks like, which is
# how sheet_import reaches 5,342 active postings that no board supplies.
NOT_A_BOARD = "gate-sheet-import"


@pytest.fixture
def population(f):
    """One posting per way a job can and cannot be reached."""
    user = f.make_user()
    for name, active in ((CATALOGUED, True), (SWITCHED_OFF, False), (UNWANTED, True)):
        f.make_source(name, active=active)
    f.subscribe(user, CATALOGUED)
    f.subscribe(user, SWITCHED_OFF)

    ids = {
        "subscribed": f.make_job(source=CATALOGUED),
        "subscribed_but_source_off": f.make_job(source=SWITCHED_OFF),
        "unsubscribed": f.make_job(source=UNWANTED),
        "not_from_a_board": f.make_job(source=NOT_A_BOARD),
        "unsubscribed_but_on_a_board": f.make_job(source=UNWANTED),
    }
    f.make_board_row(user, ids["unsubscribed_but_on_a_board"])
    return ids


def _eligible() -> set[int]:
    rows = db.query(f"SELECT j.id FROM jobs j WHERE {AI_ELIGIBLE_JOB.format(job='j')}")
    return {r["id"] for r in rows}


def _subscribed() -> set[int]:
    sql = f"SELECT j.id FROM jobs j WHERE {SUBSCRIBED_SOURCE.format(source='j.source')}"
    return {r["id"] for r in db.query(sql)}


def test_gate_admits_only_reachable_postings(population):
    assert _eligible() == {
        population["subscribed"],
        population["not_from_a_board"],
        population["unsubscribed_but_on_a_board"],
    }


def test_switching_a_source_off_stops_its_spend(population):
    """Requirement 3: deactivation must reach the sweeps, not just the scraper.

    `airtable1` was off at the source level and still cost money, because the
    sweeps read jobs.active and never looked at sources.active.
    """
    db.execute("UPDATE sources SET active = TRUE WHERE name = %s", (SWITCHED_OFF,))
    assert population["subscribed_but_source_off"] in _eligible()
    db.execute("UPDATE sources SET active = FALSE WHERE name = %s", (SWITCHED_OFF,))
    assert population["subscribed_but_source_off"] not in _eligible()


def test_subscribing_needs_no_backfill(population, f):
    """Requirement 5: the gate is evaluated per sweep, not stamped onto rows."""
    assert population["unsubscribed"] not in _eligible()
    f.subscribe(f.make_user(), UNWANTED)
    assert population["unsubscribed"] in _eligible()


def test_requirements_sweep_skips_unreachable_postings(population, f):
    """The production candidate query, not a restatement of it.

    Both postings have cached content and no stored answer, so the only thing
    that can separate them is the gate.
    """
    from api.tasks.requirements import _CANDIDATES

    urls = {}
    for key in ("subscribed", "unsubscribed"):
        row = db.query_one("SELECT url FROM jobs WHERE id = %s", (population[key],))
        assert row is not None
        urls[key] = row["url"]
        f.make_verdict(row["url"], "content", "passed", content="a long posting body " * 30)

    got = {r["url"] for r in db.query(_CANDIDATES, {"cap": 100})}
    assert urls["subscribed"] in got
    assert urls["unsubscribed"] not in got


def test_spend_splits_by_reach(client, admin_headers, population, f):
    """The admin view has to show the split, or this comes back unnoticed."""
    for key in ("subscribed", "unsubscribed"):
        row = db.query_one("SELECT url FROM jobs WHERE id = %s", (population[key],))
        assert row is not None
        f.make_verdict(row["url"], "closed", "passed")
    # A call whose url has no posting row at all. It cannot be judged either
    # way, so folding it into 'unreachable' would report work as waste on the
    # strength of a missing join.
    f.make_verdict("https://nowhere.test/gone", "closed", "passed")

    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    by_reach = {r["reach"]: r for r in body["by_reach"]}
    assert by_reach["reachable"]["calls"] == 1
    assert by_reach["unreachable"]["calls"] == 1
    assert by_reach["no_posting"]["calls"] == 1

    sources = {(r["reach"], r["source"]) for r in body["by_source_reach"]}
    assert ("unreachable", UNWANTED) in sources
    assert ("reachable", CATALOGUED) in sources


def test_scraping_stays_narrower_than_spending(population):
    """The two predicates answer different questions and must not be merged.

    SUBSCRIBED_SOURCE is the scraper's question - which boards do we fetch
    pages for. AI_ELIGIBLE_JOB is wider: a posting already on a board, or one
    a person imported, may cost tokens without the backfill queueing its page,
    because re-verification fetches those on demand. Collapsing the two widened
    the backfill's candidate pool six-fold and its query from 525ms to 7.5s.
    """
    assert _subscribed() == {population["subscribed"]}
    for key in ("not_from_a_board", "unsubscribed_but_on_a_board"):
        assert population[key] in _eligible()
        assert population[key] not in _subscribed()
