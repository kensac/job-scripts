"""handle_ingest_source: what reaches the catalog from a source row."""

from __future__ import annotations

import asyncio
import dataclasses

from api import db
from api.ai import verdicts
from core.fetching import boards
from core.fetching.posting import JobPosting
from tasks import ingest


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


def test_title_pattern_bypass_admits_every_posting_but_preserves_pattern_evidence(monkeypatch, f):
    """Bypass widens the catalog without erasing what the stored pattern would
    have done, so the experiment remains measurable and reversible."""
    f.make_source("rocketlab")
    db.execute(
        "UPDATE sources SET company = 'Rocket Lab', title_pattern = %s WHERE name = 'rocketlab'",
        (r"engineer i\b|intern|new grad",),
    )
    db.execute("UPDATE app_config SET value = 'false' WHERE key = 'source_title_patterns_enabled'")
    postings = [
        _posting("Avionics Design Engineer I"),
        _posting("Senior Avionics Design Engineer"),
        _posting("Avionics Development Intern - Electron"),
    ]
    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: postings)

    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "rocketlab"})

    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "rocketlab"}))

    titles = {r["title"] for r in db.query("SELECT title FROM jobs WHERE source = 'rocketlab'")}
    assert titles == {posting.title for posting in postings}
    evidence = {
        row["title"]: row["kept"]
        for row in db.query("SELECT title, kept FROM listings WHERE source = 'rocketlab'")
    }
    assert evidence == {
        "Avionics Design Engineer I": True,
        "Senior Avionics Design Engineer": False,
        "Avionics Development Intern - Electron": True,
    }
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["kept"] == 2
    assert progress["admitted"] == 3
    assert progress["pattern_enforced"] is False


def _failed_fetch(url: str, hours_ago: int) -> None:
    db.execute(
        "INSERT INTO page_fetches (url, status, method, created_at) "
        "VALUES (%s, 'failed', 'fetch returned nothing', now() - make_interval(hours => %s))",
        (url, hours_ago),
    )


def test_a_posting_whose_fetch_failed_today_is_not_fetched_again_this_hour(monkeypatch, f):
    """A failed fetch leaves a row and the row is the memory. Inside the retry
    window the posting is skipped; past it, tried again. The window comes from
    the persisted config, so an admin can shorten it without a deploy."""
    from api import fetching
    from core.fetching import ats

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
        "SELECT url FROM page_fetches WHERE status = 'failed' "
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


def _job_versions() -> dict[str, tuple[str, str]]:
    """Each jobs row's physical version. A new xmin means the row was
    rewritten, whatever the values in it say."""
    return {
        r["url"]: (r["xmin"], r["ctid"])
        for r in db.query("SELECT url, xmin::text AS xmin, ctid::text AS ctid FROM jobs")
    }


def _catalog_rows() -> dict[str, dict]:
    return {
        r["url"]: r
        for r in db.query(
            "SELECT url, raw_url, company, title, locations, terms, source, active, "
            "date_posted FROM jobs"
        )
    }


def _catalog_rows_ids() -> dict[str, int]:
    return {r["url"]: r["id"] for r in db.query("SELECT id, url FROM jobs")}


def test_a_repull_that_changes_nothing_leaves_every_catalog_row_untouched():
    """Rewriting every row a pull listed was 1.33M updates in 36 hours on a
    605k-row catalog and 5.7 GB of WAL (pg_stat_statements, 2026-10-03),
    against 5,336 returns in job_listing_events in the same window. The
    second pull carries only what the update would not apply: a later date
    (the first date seen is kept), a different company and title on a feed
    row (a feed row keeps the ones it was first stored with), and a raw_url
    (set on insert only)."""
    from core import catalog

    posts = [
        dataclasses.replace(p, company="Rocket Lab", date_posted=1_700_000_000)
        for p in (
            _posting("Software Engineer I"),
            dataclasses.replace(_posting("Software Engineer II"), terms=["Summer 2027"]),
            dataclasses.replace(_posting("Intern"), active=False),
        )
    ]
    catalog.upsert_postings(posts, "rocketlab")
    before, values = _job_versions(), _catalog_rows()
    assert len(before) == 3

    catalog.upsert_postings(
        [
            dataclasses.replace(
                p,
                date_posted=1_800_000_000,
                company="Rocket Lab USA",
                title=p.title + " (Remote)",
                raw_url="https://elsewhere.test/apply",
            )
            for p in posts
        ],
        "rocketlab",
    )

    assert _job_versions() == before
    assert _catalog_rows() == values


def test_a_changed_posting_rewrites_its_row_and_only_its_row():
    from core import catalog

    posts = [_posting(t) for t in ("A", "B", "C", "D", "E")]
    catalog.upsert_postings(posts, "rocketlab")
    db.execute("UPDATE jobs SET date_posted = NULL, company = '' WHERE url = %s", (posts[3].url,))
    before = _job_versions()

    moved = dataclasses.replace(posts[0], locations=["Remote"])
    retermed = dataclasses.replace(posts[1], terms=["Fall 2027"])
    closed = dataclasses.replace(posts[2], active=False)
    dated = dataclasses.replace(posts[3], date_posted=1_700_000_000, company="Rocket Lab")
    catalog.upsert_postings([moved, retermed, closed, dated, posts[4]], "rocketlab")

    after = _job_versions()
    assert {u for u in before if after[u] != before[u]} == {
        moved.url,
        retermed.url,
        closed.url,
        dated.url,
    }
    rows = _catalog_rows()
    assert rows[moved.url]["locations"] == ["Remote"]
    assert rows[retermed.url]["terms"] == ["Fall 2027"]
    assert rows[closed.url]["active"] is False
    assert rows[dated.url]["date_posted"] is not None
    assert rows[dated.url]["company"] == "Rocket Lab"


def test_a_feed_listing_an_uploaded_posting_takes_it_over_even_when_nothing_else_differs(f):
    from core import catalog

    post = _posting("Software Engineer")
    catalog.upsert_postings([post], "upload")
    job_id = _catalog_rows_ids()[post.url]
    f.upload(job_id, f.make_user(), status="pending")

    catalog.upsert_postings([post], "rocketlab")

    assert _catalog_rows()[post.url]["source"] == "rocketlab"
    status = db.query_one("SELECT status FROM posting_uploads WHERE job_id = %s", (job_id,))
    assert status == {"status": "done"}


def test_a_return_rewrites_the_row_and_records_the_event_once():
    from core import catalog

    post = _posting("Software Engineer")
    catalog.upsert_postings([post], "rocketlab")
    job_id = db.query_one("SELECT id FROM jobs WHERE url = %s", (post.url,))["id"]
    catalog.retire_unlisted("rocketlab", [])
    before = _job_versions()

    catalog.upsert_postings([post], "rocketlab")
    returned = _job_versions()
    catalog.upsert_postings([post], "rocketlab")

    assert returned[post.url] != before[post.url]
    assert _job_versions() == returned
    assert db.query_one("SELECT active FROM jobs WHERE id = %s", (job_id,))["active"]
    assert _listing_events(job_id) == [False, True]


def test_a_partial_pull_admits_what_it_saw_and_retires_nothing(monkeypatch, f):
    """A Workday tenant capped at 2,000 results is read in facet slices, and
    nothing proves the slices saw every posting. Retiring what they missed
    would close live postings; the complete pull after it still retires."""
    f.make_source("capped")
    db.execute(
        "UPDATE sources SET company = 'Capped', listings_url = %s WHERE name = 'capped'",
        ("https://capped.wd5.myworkdayjobs.com/wday/cxs/capped/External/jobs",),
    )
    seen, missed = _posting("Propulsion Engineer I"), _posting("Structures Engineer I")
    pulls = [[seen, missed], boards.PartialPull([seen]), [seen]]

    def fetch_listings(url, company=None):
        result = pulls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(boards, "fetch_listings", fetch_listings)
    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)

    def active() -> set[str]:
        rows = db.query("SELECT title FROM jobs WHERE source = 'capped' AND active")
        return {r["title"] for r in rows}

    for expected in (
        {"Propulsion Engineer I", "Structures Engineer I"},
        {"Propulsion Engineer I", "Structures Engineer I"},
        {"Propulsion Engineer I"},
    ):
        task_id = f.make_task("ingest_source", {"source": "capped"})
        asyncio.run(ingest.handle_ingest_source(task_id, {"source": "capped"}))
        assert active() == expected
