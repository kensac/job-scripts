"""handle_ingest_source: what reaches the catalog from a source row."""

from __future__ import annotations

import asyncio

from api import db, verdicts
from api.tasks import ingest
from core import boards
from core.posting import JobPosting


def _posting(title: str, date_posted: int = 0) -> JobPosting:
    return JobPosting(
        company="",
        locations=["Long Beach, CA"],
        title=title,
        url=f"https://job-boards.greenhouse.io/rocketlab/jobs/{abs(hash(title)) % 10**8}",
        terms=[],
        active=True,
        date_posted=date_posted,
        raw_url="",
    )


def test_title_pattern_keeps_the_rest_of_the_board_out_of_the_catalog(monkeypatch, f):
    """A company board lists every opening. The source's pattern is applied
    before the upsert, so the senior roles never become rows, never get their
    pages cached, and never reach verify_new."""
    f.make_source("rocketlab")
    db.execute(
        "UPDATE sources SET company = 'Rocket Lab', title_pattern = %s WHERE name = 'rocketlab'",
        (r"engineer i\b|intern|new grad",),
    )
    calls = []

    def fetch_listings(url, company=None):
        calls.append((url, company))
        return [
            _posting("Avionics Design Engineer I"),
            _posting("Senior Avionics Design Engineer"),
            _posting("Avionics Development Intern - Electron"),
        ]

    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(boards, "fetch_listings", fetch_listings)
    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "rocketlab"})

    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "rocketlab"}))

    titles = {r["title"] for r in db.query("SELECT title FROM jobs WHERE source = 'rocketlab'")}
    assert titles == {"Avionics Design Engineer I", "Avionics Development Intern - Electron"}
    # The company on the source row is what the fetcher was handed.
    assert calls == [("https://rocketlab.test/jobs.json", "Rocket Lab")]


def _failed_fetch(url: str, hours_ago: int) -> None:
    db.execute(
        "INSERT INTO ai_queries (url, check_type, status, reason, created_at) "
        "VALUES (%s, 'content', 'failed', 'fetch returned nothing', "
        "now() - make_interval(hours => %s))",
        (url, hours_ago),
    )


def test_a_posting_whose_fetch_failed_today_is_not_fetched_again_this_hour(monkeypatch, f):
    """A failed fetch leaves a row and the row is the memory. Inside the retry
    window the posting is skipped; past it, tried again. The window comes from
    the persisted config, so an admin can shorten it without a deploy."""
    from api import fetching
    from core import ats

    f.make_source("acme")
    # date_posted inside the cutoff window, or ingest never considers the page.
    today = 2_000_000_000
    fresh, stale, new = (
        _posting("Engineer I, fresh failure", today),
        _posting("Engineer I, stale failure", today),
        _posting("Engineer I, never tried", today),
    )
    _failed_fetch(fresh.url, 1)
    _failed_fetch(stale.url, 30)
    db.execute("UPDATE app_config SET value = '12' WHERE key = 'fetch_retry_after_hours'")

    fetched = []

    async def no_page(url):
        fetched.append(url)
        return None, False

    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: [fresh, stale, new])
    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)
    monkeypatch.setattr(fetching, "fetch_page", no_page)
    task_id = f.make_task("ingest_source", {"source": "acme"}, status="running")

    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "acme"}))

    assert sorted(fetched) == sorted([stale.url, new.url])
    # Both attempts that ran and found nothing left a row, so the next hour
    # skips them too.
    rows = db.query(
        "SELECT url FROM ai_queries WHERE check_type = 'content' AND status = 'failed' "
        "AND created_at > now() - interval '1 minute'"
    )
    assert sorted(r["url"] for r in rows) == sorted([stale.url, new.url])
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))
    assert progress is not None
    assert progress["progress"]["skipped_recent_failure"] == 1
    assert progress["progress"]["fetch_failed"] == 2
    assert progress["progress"]["cached"] == 0


def test_a_posting_the_feed_puts_back_is_stamped_and_reaches_the_recheck(f):
    """A closed verdict used to be permanent: demote_closed takes the board
    row away, and every reverify candidate is either a board row or a posting
    whose verdict PASSED, so nothing ever read the page again. The feed
    putting the posting back is the one event that says otherwise."""
    from core import catalog

    post = _posting("Software Engineer")
    catalog.upsert_postings([post], "rocketlab")
    verdicts.record_manual(
        url=post.url,
        check_type="closed",
        rejected=True,
        reason="posting gone",
        company="",
        job_title=post.title,
        context="test",
    )
    row = db.query_one("SELECT id, active FROM jobs WHERE url = %s", (post.url,))
    assert row and row["active"] and _listing_events(row["id"]) == []

    # The board drops it, so the pull no longer admits it.
    catalog.retire_unlisted("rocketlab", [])
    assert not db.query_one("SELECT active FROM jobs WHERE url = %s", (post.url,))["active"]
    assert _listing_events(row["id"]) == [False], "the board dropping it is an observation too"

    # And the board lists it again.
    catalog.upsert_postings([post], "rocketlab")
    assert db.query_one("SELECT active FROM jobs WHERE url = %s", (post.url,))["active"]
    assert _listing_events(row["id"]) == [False, True], "the return must be recorded"

    # Which is what puts it in front of the sweep that re-fetches the page.
    assert post.url in _relisted_candidates()

    # A fresh verdict settles it: the stamp is older than the answer now.
    verdicts.record_manual(
        url=post.url,
        check_type="closed",
        rejected=False,
        reason="posting is live",
        company="",
        job_title=post.title,
        context="test",
    )
    assert post.url not in _relisted_candidates()


def _listing_events(job_id: int) -> list[bool]:
    return [
        r["listed"]
        for r in db.query(
            "SELECT listed FROM job_listing_events WHERE job_id = %s ORDER BY id", (job_id,)
        )
    ]


def _relisted_candidates() -> set[str]:
    """The re-listed branch of the reverify candidate query, on its own."""
    from core.store import AI_ELIGIBLE_JOB

    rows = db.query(
        f"""
        SELECT j.url FROM jobs j
        WHERE j.active AND {AI_ELIGIBLE_JOB.format(job="j")}
          AND (SELECT MAX(e.at) FROM job_listing_events e
               WHERE e.job_id = j.id AND e.listed) > COALESCE(
                (SELECT MAX(q.created_at) FROM ai_queries q
                 WHERE q.url = j.url AND q.check_type = 'closed'), '-infinity')
        """
    )
    return {r["url"] for r in rows}
