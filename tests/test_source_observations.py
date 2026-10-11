"""catalog.observe: what each source said about each posting, as changes."""

from __future__ import annotations

import asyncio
import dataclasses

from api import db
from api.ai import verdicts
from core import catalog
from core.fetching import boards
from core.fetching.posting import JobPosting
from tasks import ingest

A = "https://jobs.test/a"
B = "https://jobs.test/b"


def _posting(url: str, title: str = "Engineer I", active: bool = True) -> JobPosting:
    return JobPosting(
        company="Acme",
        locations=["Remote"],
        title=title,
        url=url,
        terms=[],
        active=active,
        date_posted=1_700_000_000,
        raw_url="",
    )


def _history(source: str) -> list[tuple[str, str]]:
    return [
        (r["url"], r["kind"])
        for r in db.query(
            "SELECT j.url, o.kind FROM source_observations o JOIN jobs j ON j.id = o.job_id "
            "WHERE o.source = %s ORDER BY o.id",
            (source,),
        )
    ]


def _pull(source: str, postings: list[JobPosting], kept: set[str], absence: str | None):
    catalog.upsert_postings([p for p in postings if p.url in kept], source)
    return catalog.observe(source, None, postings, kept, absence)


def test_an_authoritative_board_records_each_change_once():
    a, b = _posting(A), _posting(B)
    assert _pull("board", [a, b], {A, B}, "unlisted") == {"appeared": 2}
    # The same pull again says nothing new.
    assert _pull("board", [a, b], {A, B}, "unlisted") == {}
    # Dropped from a complete pull, then listed again.
    assert _pull("board", [a], {A}, "unlisted") == {"unlisted": 1}
    assert _pull("board", [a], {A}, "unlisted") == {}
    assert _pull("board", [a, b], {A, B}, "unlisted") == {"reappeared": 1}
    assert _history("board") == [
        (A, "appeared"),
        (B, "appeared"),
        (B, "unlisted"),
        (B, "reappeared"),
    ]


def test_a_pattern_miss_is_filtered_never_unlisted():
    """Still listed, no longer admitted: a distinct observation, which is
    exactly what retire_unlisted cannot tell apart from a closure."""
    _pull("board", [_posting(A)], {A}, "unlisted")
    _pull("board", [_posting(A, "Senior Engineer")], set(), "unlisted")
    assert _history("board") == [(A, "appeared"), (A, "filtered")]


def test_absence_from_an_aggregator_is_not_listed_and_a_partial_pull_records_none():
    _pull("feed", [_posting(A), _posting(B)], {A, B}, "not_listed")
    # A pull that cannot show it saw everything records no absence at all.
    assert _pull("feed", [_posting(A)], {A}, None) == {}
    assert _pull("feed", [_posting(A)], {A}, "not_listed") == {"not_listed": 1}
    assert _history("feed")[-1] == (B, "not_listed")


def test_a_feed_flagging_a_posting_inactive_unlists_it():
    _pull("feed", [_posting(A)], {A}, "not_listed")
    _pull("feed", [_posting(A, active=False)], {A}, "not_listed")
    assert _history("feed") == [(A, "appeared"), (A, "unlisted")]


def test_each_source_observes_a_shared_posting_separately():
    """One url is one job; two sources listing it are two histories, and one
    dropping it says nothing about the other."""
    _pull("board", [_posting(A)], {A}, "unlisted")
    _pull("feed", [_posting(A)], {A}, "not_listed")
    _pull("board", [], set(), "unlisted")
    assert _history("board") == [(A, "appeared"), (A, "unlisted")]
    assert _history("feed") == [(A, "appeared")]
    assert db.query_one("SELECT count(*) AS n FROM jobs")["n"] == 1


def test_ingest_records_the_pull_under_its_task(monkeypatch, f):
    f.make_source("rocketlab")
    db.execute(
        "UPDATE sources SET listings_url = 'https://boards-api.greenhouse.io/v1/boards/x/jobs', "
        "title_pattern = 'engineer i' WHERE name = 'rocketlab'"
    )
    listed = [_posting(A), dataclasses.replace(_posting(B), title="Senior Engineer")]
    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: listed)

    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "rocketlab"})
    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "rocketlab"}))

    rows = db.query("SELECT kind, run_id FROM source_observations")
    # B has no catalog row (the pattern kept it out), so nothing points at it.
    assert rows == [{"kind": "appeared", "run_id": task_id}]
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["observed"] == {"appeared": 1}
    assert progress["complete"] is True


def _available(url: str, definition: str = catalog.AVAILABLE) -> bool | None:
    sql = definition.format(job="j")
    return db.query_one(f"SELECT {sql} AS a FROM jobs j WHERE j.url = %s", (url,))["a"]


def test_availability_is_read_from_the_latest_observation_of_switched_on_sources(f, set_config):
    f.make_source("board")
    f.make_source("feed")
    f.make_source("off", active=False)
    _pull("board", [_posting(A)], {A}, "unlisted")
    assert _available(A) is True
    _pull("board", [], set(), "unlisted")
    assert _available(A) is False, "a complete authoritative pull left it out"
    # An aggregator that stops listing it keeps it available: absence there
    # says nothing about whether it closed.
    _pull("feed", [_posting(A)], {A}, "not_listed")
    _pull("feed", [], set(), "not_listed")
    assert _available(A) is True
    # Listed only by a switched-off source: not available.
    _pull("off", [_posting(B)], {B}, "unlisted")
    assert _available(B) is False
    # Filtered admits only while patterns are not enforced, read from the
    # switch where availability is read.
    _pull("board", [_posting(A, "Senior")], set(), "unlisted")
    db.execute("UPDATE sources SET active = false WHERE name = 'feed'")
    set_config("source_title_patterns_enabled", True)
    assert _available(A) is False
    set_config("source_title_patterns_enabled", False)
    assert _available(A) is True


def test_an_unseeded_enforcement_switch_reads_as_its_seeded_default():
    # PATTERNS_ENFORCED spells the default in SQL; it must be api.config's.
    from api.config import CONFIG_KEYS

    db.execute("DELETE FROM app_config WHERE key = 'source_title_patterns_enabled'")
    enforced = db.query_one(f"SELECT {catalog.PATTERNS_ENFORCED} AS e")["e"]
    assert enforced is CONFIG_KEYS["source_title_patterns_enabled"].default


def test_a_job_no_source_has_observed_is_cannot_tell(f):
    f.make_job(url=A)
    assert _available(A) is None


def test_a_sheet_import_is_available_only_while_a_switched_on_source_lists_it(f):
    f.make_job(url=A, source="sheet_import")
    assert _available(A) is False
    f.make_source("board")
    _pull("board", [_posting(A)], {A}, "unlisted")
    assert _available(A) is True


def test_a_row_no_observation_decides_stores_its_last_known_feed_state(f):
    """A partial pull never observes the rows past its cap, and a board not
    pulled yet has observed nothing: those keep what their feed last said."""
    f.make_job(url=A, active=True)
    f.make_job(url=B, active=False)
    db.execute("UPDATE jobs SET available = NULL")
    catalog.reconcile_available()
    assert (_stored(A), _stored(B)) == (True, False)
    f.make_source("board")
    _pull("board", [_posting(B)], {B}, "unlisted")
    assert _available(B, catalog.IS_AVAILABLE) is True, "observed: the observation decides"


def test_readers_read_a_null_as_not_available_whatever_the_feed_flag_says(f):
    """IS_AVAILABLE reads the stored column alone: no fallback to jobs.active.
    NULL is a row nothing has stored yet, which the reconcile fills."""
    f.make_job(url=A, active=True)
    db.execute("UPDATE jobs SET available = NULL")
    assert _available(A, catalog.IS_AVAILABLE) is False
    catalog.reconcile_available()
    assert _available(A, catalog.IS_AVAILABLE) is True


def test_an_administrators_correction_holds_until_a_source_says_something_new(f):
    f.make_source("board")
    _pull("board", [_posting(A)], {A}, "unlisted")
    job_id = db.query_one("SELECT id FROM jobs WHERE url = %s", (A,))["id"]
    assert catalog.set_active(job_id, False)
    assert _available(A) is False
    # The board lists it as before: nothing new, the correction stands.
    _pull("board", [_posting(A)], {A}, "unlisted")
    assert _available(A) is False
    # The board drops it and lists it again: that is news, and it wins.
    _pull("board", [], set(), "unlisted")
    _pull("board", [_posting(A)], {A}, "unlisted")
    assert _available(A) is True
    assert catalog.set_active(job_id, False)
    assert catalog.set_active(job_id, True)
    assert _available(A) is True
    assert not catalog.set_active(job_id + 999, False)


def test_the_shadow_counts_each_disagreement_once(f):
    f.make_source("board")
    _pull("board", [_posting(A), _posting(B)], {A, B}, "unlisted")
    # The legacy flag says closed where the observations say listed.
    db.execute("UPDATE jobs SET active = false WHERE url = %s", (B,))
    cells = ingest._summarise(catalog.availability_shadow())
    assert {k: v["n"] for k, v in cells.items()} == {
        "legacy=True projected=True": 1,
        "legacy=False projected=True": 1,
    }
    assert cells["legacy=False projected=True"]["sources"] == {"board": 1}


def _stored(url: str) -> bool | None:
    return db.query_one("SELECT available FROM jobs WHERE url = %s", (url,))["available"]


def test_the_stored_projection_follows_observations_corrections_and_switches(f):
    f.make_source("board")
    _pull("board", [_posting(A)], {A}, "unlisted")
    assert _stored(A) is True, "observe writes what it changed"
    _pull("board", [], set(), "unlisted")
    assert _stored(A) is False
    job_id = db.query_one("SELECT id FROM jobs WHERE url = %s", (A,))["id"]
    catalog.set_active(job_id, True)
    assert _stored(A) is True, "a correction writes it in its transaction"
    _pull("board", [_posting(A)], {A}, "unlisted")
    # A switch changes availability without an observation: the reconcile
    # catches it, and writes nothing the next time.
    db.execute("UPDATE sources SET active = false WHERE name = 'board'")
    assert _stored(A) is True
    assert catalog.reconcile_available() == 1
    assert _stored(A) is False
    assert catalog.reconcile_available() == 0
    # Every row agrees with what is stored afterwards, NULL included.
    f.make_job(url=B)
    catalog.reconcile_available()
    available = catalog._STORED.format(job="j")
    assert (
        db.query_one(
            f"SELECT count(*) AS n FROM jobs j WHERE j.available IS DISTINCT FROM {available}"
        )["n"]
        == 0
    )
    assert _stored(B) is True, "no observation decides: its feed state"
