"""The re-verification sweep: which postings it takes, and what it does when
the page will not come back.

handle_reverify_open is the splitter, and it had no test of its own. Both
defects found in this file were the same shape - a verdict written once and
never revisited - and both live in the candidate query, which is the one part
of the sweep that decides whether a posting is ever looked at again.

Everything the splitter decides shows up in the rows it hands the sweep, so
standing in for the sweep is what makes the decision observable without a
provider. The gather loop is exercised through the real fetch path, with the
submission boundary recorded rather than crossed: what a sweep is willing to
pay for is the decision, and the provider is not part of it.
"""

from __future__ import annotations

import asyncio

import pytest

from api import db, fetching
from core.fetching import ats as core_ats
from tasks import runtime as tasks_runtime
from tasks import verify as tasks_verify


@pytest.fixture
def taken(monkeypatch):
    """What the splitter hands to the sweep, per call."""
    calls: list[dict] = []

    async def fake_reverify(task_id, rows, parent_id=None, force=False):
        calls.append({"rows": list(rows), "parent_id": parent_id, "force": force})

    monkeypatch.setattr(tasks_verify, "_reverify_jobs", fake_reverify)
    return calls


def _urls(calls) -> set[str]:
    return {r["url"] for call in calls for r in call["rows"]}


def _age_closed_verdict(url: str, days: int) -> None:
    """Move a verdict back on the DATABASE's clock. A verdict aged from Python
    is compared against now() by the staleness predicate, which is two clocks."""
    db.execute(
        "UPDATE ai_queries SET created_at = now() - make_interval(days => %s) "
        "WHERE url = %s AND check_type = 'closed'",
        (days, url),
    )


def _relist(job_id: int, source: str) -> None:
    db.execute(
        "INSERT INTO job_listing_events (job_id, source, listed) VALUES (%s, %s, true)",
        (job_id, source),
    )


# ---------------------------------------------------------------------------
# handle_reverify_open: which postings become candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_takes_stale_board_rows_and_postings_a_feed_put_back(f, taken):
    """Two branches, and the second is the one that made a closure permanent:
    demote_closed removes the board row, so a closed posting can never come
    back through the staleness branch that reads board rows."""
    source = f.make_source("sweep-src")
    uid = f.make_user()
    f.subscribe(uid, source)

    stale_id, stale_url = f.make_ready_job(source=source)
    _age_closed_verdict(stale_url, 30)
    f.make_board_row(uid, stale_id, status=None)

    fresh_id, fresh_url = f.make_ready_job(source=source)
    f.make_board_row(uid, fresh_id, status=None)

    touched_id, touched_url = f.make_ready_job(source=source)
    _age_closed_verdict(touched_url, 30)
    f.make_board_row(uid, touched_id, status="Applied")

    # Closed, so it has no board row at all, and its feed has since listed it.
    relisted_id, relisted_url = f.make_ready_job(source=source, closed="rejected")
    _age_closed_verdict(relisted_url, 2)
    _relist(relisted_id, source)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})

    assert _urls(taken) == {stale_url, relisted_url}
    assert fresh_url not in _urls(taken), "a verdict inside the window is not stale"
    assert touched_url not in _urls(taken), "a row the person set is theirs, not the sweep's"


@pytest.mark.asyncio
async def test_a_relisting_older_than_the_verdict_answering_it_is_not_a_candidate(f, taken):
    """Self-clearing, and this is what clears it: the fresh verdict is newer
    than the return. Without the comparison the posting is re-checked every
    cycle for the rest of its life."""
    source = f.make_source("settled-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    job_id, url = f.make_ready_job(source=source, closed="rejected")
    _age_closed_verdict(url, 2)
    _relist(job_id, source)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})
    assert _urls(taken) == {url}

    taken.clear()
    f.make_verdict(url, "closed", "passed")  # the sweep answers
    await tasks_verify.handle_reverify_open(task_id, {})
    assert _urls(taken) == set()


@pytest.mark.asyncio
async def test_a_full_run_takes_only_postings_believed_open_and_reachable(f, taken):
    """A full run reads `jobs` directly rather than board rows, which is what
    made it the last way an unreachable posting could cost money (#303)."""
    subscribed = f.make_source("full-src")
    unsubscribed = f.make_source("nobody-wants-this")
    uid = f.make_user()
    f.subscribe(uid, subscribed)

    _, open_url = f.make_ready_job(source=subscribed)
    _, closed_url = f.make_ready_job(source=subscribed, closed="rejected")
    _, inactive_url = f.make_ready_job(source=subscribed, active=False)
    _, unreachable_url = f.make_ready_job(source=unsubscribed)
    # Nobody subscribes to its source either, but a person kept it, so it is
    # theirs now and stays answerable.
    kept_id, kept_url = f.make_ready_job(source=unsubscribed)
    f.make_board_row(uid, kept_id, status="Saved")
    # A source no board supplies (a sheet import, an upload) has no
    # subscription to look for and must not fall through the gate.
    _, imported_url = f.make_ready_job(source="sheet_import")

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {"full": True})

    assert _urls(taken) == {open_url, kept_url, imported_url}
    assert closed_url not in _urls(taken), "a full run asks for verdicts that passed"
    assert inactive_url not in _urls(taken)
    assert unreachable_url not in _urls(taken), "no user can open it, so no tokens go to it"
    assert taken[0]["force"] is True, "a forced sweep must not skip a verdict made today"


@pytest.mark.asyncio
async def test_the_per_cycle_cap_bounds_the_stale_branch_and_not_the_relistings(
    f, taken, monkeypatch
):
    """The cap exists to bound a sweep over everything that went stale at once.
    A re-listing costs one check per posting a feed actually put back, so the
    cap must not displace one; moving the LIMIT outside the UNION would."""
    monkeypatch.setattr(tasks_verify, "REVERIFY_PER_CYCLE", 1)
    source = f.make_source("cap-src")
    uid = f.make_user()
    f.subscribe(uid, source)

    stale_urls = set()
    for _ in range(3):
        job_id, url = f.make_ready_job(source=source)
        _age_closed_verdict(url, 30)
        f.make_board_row(uid, job_id, status=None)
        stale_urls.add(url)

    relisted_id, relisted_url = f.make_ready_job(source=source, closed="rejected")
    _age_closed_verdict(relisted_url, 2)
    _relist(relisted_id, source)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})

    selected = _urls(taken)
    assert relisted_url in selected
    assert len(selected & stale_urls) == 1, "the cap bounds the stale branch"


@pytest.mark.asyncio
async def test_nothing_stale_still_demotes_the_rows_a_closure_left_behind(f, taken):
    """demote_closed runs on every path out of the splitter. Hanging it off
    the work would leave a closed posting on a board for as long as nothing
    else went stale."""
    source = f.make_source("demote-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    job_id, _url = f.make_ready_job(source=source, closed="rejected")
    f.make_board_row(uid, job_id, status=None)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})

    assert taken == [], "a verdict recorded today is not stale"
    assert not db.query_one("SELECT 1 FROM user_jobs WHERE job_id = %s", (job_id,))


@pytest.mark.asyncio
async def test_more_candidates_than_a_chunk_are_sharded_without_loss(f, taken, monkeypatch):
    """Every candidate lands in exactly one chunk. A partition that drops or
    repeats a row is invisible in production: the cycle just re-checks, or
    never checks, a posting nobody is watching."""
    monkeypatch.setattr(tasks_verify, "CHUNK_SIZE", 2)
    source = f.make_source("chunk-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    expected = set()
    for _ in range(5):
        job_id, url = f.make_ready_job(source=source)
        _age_closed_verdict(url, 30)
        f.make_board_row(uid, job_id, status=None)
        expected.add(url)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {"full": False})

    assert taken == [], "past a chunk the splitter shards instead of sweeping inline"
    chunks = db.query(
        "SELECT payload FROM tasks WHERE kind = 'reverify_chunk' ORDER BY id",
    )
    assert len(chunks) == 3
    sharded = [r["url"] for c in chunks for r in c["payload"]["rows"]]
    assert sorted(sharded) == sorted(expected), "no candidate lost, none checked twice"
    assert all(c["payload"]["parent_id"] == task_id for c in chunks)

    parent = db.query_one("SELECT status, progress FROM tasks WHERE id = %s", (task_id,))
    assert parent["status"] == "waiting"
    assert parent["progress"]["total"] == len(expected)


@pytest.mark.asyncio
async def test_a_chunk_carries_the_forced_flag_the_splitter_gave_it(f, taken):
    """A full run is sharded like any other, and force is what makes it full.
    A chunk that lost the flag would skip every verdict made today, which is
    the whole population a full run exists to overturn."""
    rows = [{"url": "https://chunked.test/1", "company": "C", "title": "T"}]
    task_id = f.make_task("reverify_chunk", {}, status="running")

    await tasks_verify.handle_reverify_chunk(
        task_id, {"parent_id": 42, "rows": rows, "force": True}
    )
    assert taken == [{"rows": rows, "parent_id": 42, "force": True}]


@pytest.mark.asyncio
async def test_a_splitter_resuming_a_parked_batch_selects_no_new_candidates(f, taken):
    """The splitter is re-entered when its batch comes back. Re-running the
    candidate query there would submit a second batch for postings the first
    one is still answering, and pay for both."""
    source = f.make_source("resume-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    job_id, url = f.make_ready_job(source=source)
    _age_closed_verdict(url, 30)
    f.make_board_row(uid, job_id, status=None)

    task_id = f.make_task("reverify_open", {"batch_ids": ["batch_parked"]}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})

    assert taken == [{"rows": [], "parent_id": None, "force": False}]
    assert _urls(taken) == set(), "the stale posting waits for the batch in flight"


# ---------------------------------------------------------------------------
# _reverify_jobs: what the sweep is willing to pay for
# ---------------------------------------------------------------------------


@pytest.fixture
def submitted(monkeypatch):
    """The urls that crossed the submission boundary, per call."""
    calls: list[list[str]] = []

    async def fake_submit(task_id, specs, *args, **kwargs):
        calls.append([spec.custom_id for spec in specs])
        return []

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(core_ats, "resolve", lambda url: core_ats.UNSUPPORTED)
    monkeypatch.setattr(tasks_verify, "submit_or_collect", fake_submit)
    return calls


@pytest.mark.asyncio
async def test_a_page_that_will_not_come_back_keeps_its_verdict(f, submitted, monkeypatch):
    """A fetch that fails says nothing about the job. Judging the posting on
    an empty page would close a live one; paying for the call would buy the
    same nothing. The failure is still recorded, as a failed content row: that
    is the memory the next cycle waits on, and it is not a verdict."""

    async def blank(url):
        return "", False

    monkeypatch.setattr(fetching, "fetch_page", blank)
    _, url = f.make_ready_job(source=f.make_source("gone-src"))
    _age_closed_verdict(url, 30)

    task_id = f.make_task("reverify_chunk", {}, status="running")
    await tasks_verify._reverify_jobs(task_id, [{"url": url, "company": "C", "title": "T"}])

    assert submitted == [], "an empty page is not worth a call"
    closed = db.query_one(
        "SELECT status, config_name FROM ai_queries WHERE url = %s AND check_type = 'closed' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert closed["status"] == "passed", "the prior verdict stands"
    assert closed["config_name"] != "reverify", "and the sweep did not write over it"
    failure = db.query_one(
        "SELECT status, input_content FROM ai_queries WHERE url = %s AND check_type = 'content' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert (failure["status"], failure["input_content"]) == ("failed", None)


@pytest.mark.asyncio
async def test_one_posting_failing_to_fetch_does_not_take_the_sweep_with_it(
    f, submitted, monkeypatch
):
    """A chunk is a hundred postings on one worker. One host refusing a
    connection must cost its own posting and nothing else."""

    async def fetch(url):
        if "boom" in url:
            raise RuntimeError("connection reset by peer")
        return "a long job description " * 40, False

    monkeypatch.setattr(fetching, "fetch_page", fetch)
    src = f.make_source("mixed-src")
    _, bad = f.make_ready_job(source=src, url="https://boom.test/1")
    _, good = f.make_ready_job(source=src, url="https://fine.test/1")
    for url in (bad, good):
        _age_closed_verdict(url, 30)

    task_id = f.make_task("reverify_chunk", {}, status="running")
    await tasks_verify._reverify_jobs(
        task_id,
        [
            {"url": bad, "company": "C", "title": "T"},
            {"url": good, "company": "C", "title": "T"},
        ],
    )

    assert submitted == [[good]]


@pytest.mark.asyncio
async def test_a_cancelled_sweep_stops_before_it_submits(f, submitted, monkeypatch):
    """Cancelling is how a person stops a run that is spending. A sweep that
    submitted anyway would bill the whole chunk after the stop."""
    completed: list[str] = []

    async def fetch(url):
        if "slow" in url:
            # Long enough to still be in flight when the stop is seen. A sweep
            # that ignores the stop waits it out and then fails the assertions
            # below, rather than hanging.
            await asyncio.sleep(5)
        completed.append(url)
        return "a long job description " * 40, False

    monkeypatch.setattr(fetching, "fetch_page", fetch)
    src = f.make_source("cancel-src")
    task_id = f.make_task("reverify_chunk", {}, status="running")
    rows = []
    for name in ("fast-1", "fast-2", "slow-1"):
        _, url = f.make_ready_job(source=src, url=f"https://{name}.test/1")
        _age_closed_verdict(url, 30)
        rows.append({"url": url, "company": "C", "title": "T"})

    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (task_id,))
    await tasks_verify._reverify_jobs(task_id, rows)

    assert tasks_runtime.cancelled(task_id)
    assert submitted == [], "nothing gathered before the stop is paid for after it"
    assert "slow" not in " ".join(completed), "the fetch in flight was cancelled, not awaited"
