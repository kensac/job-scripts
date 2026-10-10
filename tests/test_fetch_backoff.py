"""A page fetch that keeps coming back empty is retried less often each time,
and after enough consecutive failures not at all by anything automatic.

On 2026-10-04 production held 3,511 postings that had never fetched once,
31,433 failed content rows behind them (9 a posting on average, 50 at most),
because every caller retried a failure on the same flat 24 hours forever.
The memory is still the failed content rows; what changed is that the run of
them since the last success decides the wait, and the length of the run
decides when to stop.
"""

from __future__ import annotations

import asyncio

import pytest

from api import ai, db, fetching
from api.ai import verdicts
from api.model_calls import Payer
from core.fetching import ats


def _failures(url: str, *hours_ago: int) -> None:
    for h in hours_ago:
        db.execute(
            "INSERT INTO page_fetches (url, status, method, created_at) "
            "VALUES (%s, 'failed', 'fetch returned nothing', now() - make_interval(hours => %s))",
            (url, h),
        )


def _success(url: str, hours_ago: int) -> None:
    db.execute(
        "INSERT INTO page_fetches (url, status, method, content, created_at) "
        "VALUES (%s, 'passed', 'scraped', %s, now() - make_interval(hours => %s))",
        (url, "page text " * 40, hours_ago),
    )


def _config(**values: int) -> None:
    for key, value in values.items():
        db.execute("UPDATE app_config SET value = %s WHERE key = %s", (db.jsonb(value), key))


def _given_up(url: str) -> None:
    """Exactly the threshold, the last one long ago, so only the give-up rule
    can be what parks it: no backoff window reaches back 10,000 hours."""
    _config(fetch_give_up_after_failures=3)
    _failures(url, 10_000, 10_001, 10_002)


def test_the_wait_doubles_with_each_consecutive_failure_up_to_the_cap():
    _config(fetch_retry_after_hours=24, fetch_retry_max_hours=100, fetch_give_up_after_failures=50)
    one_due = "https://b.test/one-due"  # 1 failure: waits 24 h
    two_waiting = "https://b.test/two-waiting"  # 2 failures: waits 48 h
    two_due = "https://b.test/two-due"
    three_waiting = "https://b.test/three-waiting"  # 3 failures: waits 96 h
    capped_due = "https://b.test/capped-due"  # 6 failures: 768 h, capped at 100
    _failures(one_due, 30)
    _failures(two_waiting, 30, 200)
    _failures(two_due, 50, 200)
    _failures(three_waiting, 90, 200, 300)
    _failures(capped_due, 101, 200, 300, 400, 500, 600)

    urls = [one_due, two_waiting, two_due, three_waiting, capped_due, "https://b.test/never"]
    assert verdicts.fetch_parked_urls(urls) == {two_waiting, three_waiting}


def test_a_success_resets_the_run():
    _config(fetch_retry_after_hours=24, fetch_retry_max_hours=1000, fetch_give_up_after_failures=3)
    url = "https://b.test/recovered"
    _failures(url, 500, 400, 300)  # a whole give-up's worth, then
    _success(url, 200)  # the page came back once
    _failures(url, 30)  # and failed once since: the first wait, 24 h

    assert verdicts.fetch_parked_urls([url]) == set()
    assert verdicts.fetch_failure_streaks([url]) == {url: 1}


def test_the_tunables_are_read_from_app_config():
    url = "https://b.test/tunable"
    _failures(url, 3, 100)  # two failures, the last 3 h ago
    _config(fetch_retry_after_hours=24, fetch_retry_max_hours=1000, fetch_give_up_after_failures=9)
    assert verdicts.fetch_parked_urls([url]) == {url}
    _config(fetch_retry_after_hours=1)  # waits 2 h now
    assert verdicts.fetch_parked_urls([url]) == set()
    _config(fetch_retry_after_hours=24, fetch_retry_max_hours=2)  # the cap binds
    assert verdicts.fetch_parked_urls([url]) == set()
    _config(fetch_give_up_after_failures=2)
    assert verdicts.fetch_parked_urls([url]) == {url}


def test_the_tunables_are_registered_with_their_defaults(client, admin_headers):
    config = client.get("/v1/admin/config", headers=admin_headers).json()["config"]
    assert config["fetch_retry_after_hours"] == 24
    assert config["fetch_retry_max_hours"] == 168
    assert config["fetch_give_up_after_failures"] == 6


def test_ingest_does_not_fetch_a_given_up_posting(monkeypatch, f):
    from core.fetching import boards
    from core.fetching.posting import JobPosting
    from tasks import ingest

    f.make_source("acme")
    posting = JobPosting(
        company="Acme",
        locations=[],
        title="Engineer I",
        url="https://b.test/ingest-dead",
        terms=[],
        active=True,
        date_posted=2_000_000_000,
        raw_url="",
    )
    _given_up(posting.url)
    fetched = []

    async def no_page(url):
        fetched.append(url)
        return None, False

    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: [posting])
    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)
    monkeypatch.setattr(fetching, "fetch_page", no_page)
    task_id = f.make_task("ingest_source", {"source": "acme"}, status="running")

    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "acme"}))

    assert fetched == []


@pytest.mark.asyncio
async def test_the_content_backfill_does_not_fetch_a_given_up_posting(monkeypatch, f):
    from tasks import content

    source = f.make_source("bf-src")
    f.subscribe(f.make_user(), source)
    f.make_job(url="https://b.test/bf-dead", source=source)
    f.make_job(url="https://b.test/bf-new", source=source)
    _given_up("https://b.test/bf-dead")
    fetched = []

    async def no_page(url):
        fetched.append(url)
        return None, False

    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)
    monkeypatch.setattr(fetching, "fetch_page", no_page)

    await content.handle_fetch_missing_content(f.make_task("fetch_missing_content"), {})

    assert fetched == ["https://b.test/bf-new"]


@pytest.mark.asyncio
async def test_the_content_backfill_reads_the_latest_closed_answer(monkeypatch, f):
    """Gone, then listed again: the board shows it open, so it has a page."""
    from tasks import content

    source = f.make_source("bf-src")
    f.subscribe(f.make_user(), source)
    f.make_job(url="https://b.test/reopened", source=source)
    f.make_verdict("https://b.test/reopened", "closed", "rejected")
    f.make_verdict("https://b.test/reopened", "closed", "passed")
    f.make_job(url="https://b.test/gone", source=source)
    f.make_verdict("https://b.test/gone", "closed", "passed")
    f.make_verdict("https://b.test/gone", "closed", "rejected")
    fetched = []

    async def no_page(url):
        fetched.append(url)
        return None, False

    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)
    monkeypatch.setattr(fetching, "fetch_page", no_page)

    await content.handle_fetch_missing_content(f.make_task("fetch_missing_content"), {})

    assert fetched == ["https://b.test/reopened"]


@pytest.mark.asyncio
async def test_filter_preparation_does_not_fetch_a_given_up_posting(f):
    from tasks import filter_execution

    _given_up("https://b.test/prep-dead")
    fetched = []

    async def refresh(url, **kw):
        fetched.append(url)
        return None, None

    jobs = [{"url": "https://b.test/prep-dead"}, {"url": "https://b.test/prep-new"}]
    await filter_execution.prepare_content(
        f.make_task("run_filter_batch_chunk"),
        jobs,
        cancelled=lambda: False,
        refresh_page=refresh,
    )

    assert fetched == ["https://b.test/prep-new"]


@pytest.mark.asyncio
async def test_a_live_filter_run_does_not_fetch_a_given_up_posting(f, monkeypatch):
    from tasks import filter_execution

    _given_up("https://b.test/live-dead")
    fetched = []

    async def refresh(url, **kw):
        fetched.append(url)
        return None, None

    monkeypatch.setattr(filter_execution.verdicts, "refresh_page", refresh)
    hooks = filter_execution.ExecutionHooks(
        verdict_label="managed:test",
        key_source="owner",
        payer=Payer(user_id=1),
        purpose="filter",
        record_usage=lambda usage, model, batched: None,
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda done, total, label: None,
        complete=lambda: None,
    )
    await filter_execution.execute_live(
        f.make_task("run_managed_board"),
        ai.AIConfig("openai", "key", "owner", "gpt-5.6-luna"),
        filter_execution.FilterSnapshot("managed", "prompt", "filter", "hash"),
        [
            {"url": "https://b.test/live-dead", "company": "C", "title": "T"},
            {"url": "https://b.test/live-new", "company": "C", "title": "T"},
        ],
        hooks,
    )

    assert fetched == ["https://b.test/live-new"]


@pytest.mark.asyncio
async def test_reverify_does_not_fetch_a_given_up_posting(f, monkeypatch):
    from tasks import verify

    _given_up("https://b.test/rv-dead")
    fetched = []

    async def no_page(url):
        fetched.append(url)
        return None, False

    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)
    monkeypatch.setattr(fetching, "fetch_page", no_page)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    rows = [
        {"url": "https://b.test/rv-dead", "company": "C", "title": "T"},
        {"url": "https://b.test/rv-new", "company": "C", "title": "T"},
    ]

    await verify._reverify_jobs(f.make_task("reverify_chunk", status="running"), rows)

    assert fetched == ["https://b.test/rv-new"]


def test_an_admin_recheck_still_fetches_a_given_up_posting(client, admin_headers, f, monkeypatch):
    url = "https://b.test/manual"
    job_id = f.make_job(url=url)
    _given_up(url)
    fetched = []

    async def page(u):
        fetched.append(u)
        return None, False

    monkeypatch.setattr(ats, "resolve", lambda u: ats.UNSUPPORTED)
    monkeypatch.setattr(fetching, "fetch_page", page)

    r = client.post(
        "/v1/admin/checks/run", json={"job_id": job_id, "check": "closed"}, headers=admin_headers
    )

    assert r.status_code == 409, r.text
    assert fetched == [url]


def test_the_admin_jobs_list_and_timeline_say_a_posting_is_unfetchable(client, admin_headers):
    dead, flaky = "https://b.test/admin-dead", "https://b.test/admin-flaky"
    _given_up(dead)
    _failures(flaky, 1)

    rows = {r["url"]: r for r in client.get("/v1/admin/jobs", headers=admin_headers).json()["rows"]}
    assert rows[dead]["verdict"] == "unfetchable"
    assert rows[dead]["unfetchable"] is True
    assert rows[dead]["content_failures"] == 3
    assert rows[flaky]["verdict"] == "other"
    assert rows[flaky]["unfetchable"] is False
    assert rows[flaky]["content_failures"] == 1

    timeline = client.get(
        "/v1/admin/jobs/timeline", params={"url": dead}, headers=admin_headers
    ).json()
    assert timeline["unfetchable"] is True
    assert timeline["content_failures"] == 3
