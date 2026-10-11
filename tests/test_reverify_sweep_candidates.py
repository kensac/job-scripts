"""Which postings the re-verification splitter selects and shards."""

from __future__ import annotations

import pytest

from api import db
from core import catalog
from tasks import verify as tasks_verify
from tasks.verification import reverification
from tests.reverify_sweep_support import age_closed_verdict


@pytest.fixture
def taken(monkeypatch):
    """What the splitter hands to the sweep, per call."""
    calls: list[dict] = []

    async def fake_reverify(task_id, rows, parent_id=None, force=False):
        calls.append({"rows": list(rows), "parent_id": parent_id, "force": force})

    monkeypatch.setattr(reverification, "_reverify_jobs", fake_reverify)
    return calls


def _urls(calls) -> set[str]:
    return {r["url"] for call in calls for r in call["rows"]}


def _machine_row(uid: int, job_id: int) -> None:
    """What materialize_passing writes for a passing posting: the
    working-set pair the sweep reads."""
    db.execute("INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)", (uid, job_id))


def _relist(job_id: int, source: str) -> None:
    db.execute(
        "INSERT INTO source_observations (job_id, source, kind) VALUES (%s, %s, 'reappeared')",
        (job_id, source),
    )


@pytest.mark.asyncio
async def test_the_sweep_takes_stale_board_rows_and_postings_a_feed_put_back(f, taken):
    """Two branches, and the second is the one that made a closure permanent:
    demote_closed removes the board row, so a closed posting can never come
    back through the staleness branch that reads board rows."""
    source = f.make_source("sweep-src")
    uid = f.make_user()
    f.subscribe(uid, source)

    stale_id, stale_url = f.make_ready_job(source=source)
    age_closed_verdict(stale_url, 30)
    _machine_row(uid, stale_id)

    fresh_id, fresh_url = f.make_ready_job(source=source)
    _machine_row(uid, fresh_id)

    touched_id, touched_url = f.make_ready_job(source=source)
    age_closed_verdict(touched_url, 30)
    _machine_row(uid, touched_id)
    f.make_board_row(uid, touched_id, status="Applied")

    # Closed, so it has no board row at all, and its feed has since listed it.
    relisted_id, relisted_url = f.make_ready_job(source=source, closed="rejected")
    age_closed_verdict(relisted_url, 2)
    _relist(relisted_id, source)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})

    assert _urls(taken) == {stale_url, relisted_url}
    assert fresh_url not in _urls(taken), "a verdict inside the window is not stale"
    assert touched_url not in _urls(taken), "a row the person set is theirs, not the sweep's"


@pytest.mark.asyncio
async def test_the_stale_branch_reads_the_working_set_not_legacy_rows(f, taken):
    """Phase 2b: an all-default user_jobs row is working-set membership, and
    the split backfill copies each into user_job_working_set. So a pair the
    working set holds is swept even when its user_jobs row is gone, and a
    bare user_jobs row the working set does not hold is not."""
    source = f.make_source("ws-src")
    uid = f.make_user()
    f.subscribe(uid, source)

    picked_id, picked_url = f.make_ready_job(source=source)
    age_closed_verdict(picked_url, 30)
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)", (uid, picked_id)
    )

    bare_id, bare_url = f.make_ready_job(source=source)
    age_closed_verdict(bare_url, 30)
    f.make_board_row(uid, bare_id, status=None)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})
    assert _urls(taken) == {picked_url}


@pytest.mark.asyncio
async def test_a_relisting_older_than_the_verdict_answering_it_is_not_a_candidate(f, taken):
    """Self-clearing, and this is what clears it: the fresh verdict is newer
    than the return. Without the comparison the posting is re-checked every
    cycle for the rest of its life."""
    source = f.make_source("settled-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    job_id, url = f.make_ready_job(source=source, closed="rejected")
    age_closed_verdict(url, 2)
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
    _, closed_since_url = f.make_ready_job(source=subscribed)
    f.make_verdict(closed_since_url, "closed", "rejected")
    _, inactive_url = f.make_ready_job(source=subscribed, active=False)
    _, unreachable_url = f.make_ready_job(source=unsubscribed)
    # Nobody subscribes to its source either, but a person kept it, so it is
    # theirs now and stays answerable.
    kept_id, kept_url = f.make_ready_job(source=unsubscribed)
    f.make_board_row(uid, kept_id, status="Saved")
    # A source no board supplies (an upload) has no subscription to look for
    # and must not fall through the gate.
    _, imported_url = f.make_ready_job(source="upload")
    # sheet_import is a switched-off source (catalog.AVAILABLE): not available,
    # so nothing spends on it.
    sheet_id, sheet_url = f.make_ready_job(source="sheet_import")
    # What a refresh stores for it (catalog.AVAILABLE).
    catalog.refresh_available([sheet_id])

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {"full": True})

    assert _urls(taken) == {open_url, kept_url, imported_url}
    assert closed_url not in _urls(taken), "a full run asks for verdicts that passed"
    assert closed_since_url not in _urls(taken), "the latest closed answer decides"
    assert inactive_url not in _urls(taken)
    assert sheet_url not in _urls(taken)
    assert unreachable_url not in _urls(taken), "no user can open it, so no tokens go to it"
    assert taken[0]["force"] is True, "a forced sweep must not skip a verdict made today"


@pytest.mark.asyncio
async def test_the_per_cycle_cap_bounds_the_stale_branch_and_not_the_relistings(
    set_config, f, taken, monkeypatch
):
    """The cap exists to bound a sweep over everything that went stale at once.
    A re-listing costs one check per posting a feed actually put back, so the
    cap must not displace one; moving the LIMIT outside the UNION would."""
    set_config("reverify_per_cycle", 1)
    source = f.make_source("cap-src")
    uid = f.make_user()
    f.subscribe(uid, source)

    stale_urls = set()
    for _ in range(3):
        job_id, url = f.make_ready_job(source=source)
        age_closed_verdict(url, 30)
        _machine_row(uid, job_id)
        stale_urls.add(url)

    relisted_id, relisted_url = f.make_ready_job(source=source, closed="rejected")
    age_closed_verdict(relisted_url, 2)
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
    _machine_row(uid, job_id)

    task_id = f.make_task("reverify_open", {}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})

    assert taken == [], "a verdict recorded today is not stale"
    assert not db.query_one("SELECT 1 FROM user_job_working_set WHERE job_id = %s", (job_id,))


@pytest.mark.asyncio
async def test_more_candidates_than_a_chunk_are_sharded_without_loss(
    set_config, f, taken, monkeypatch
):
    """Every candidate lands in exactly one chunk. A partition that drops or
    repeats a row is invisible in production: the cycle just re-checks, or
    never checks, a posting nobody is watching."""
    set_config("filter_chunk_size", 2)
    source = f.make_source("chunk-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    expected = set()
    for _ in range(5):
        job_id, url = f.make_ready_job(source=source)
        age_closed_verdict(url, 30)
        _machine_row(uid, job_id)
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
    age_closed_verdict(url, 30)
    _machine_row(uid, job_id)

    task_id = f.make_task("reverify_open", {"batch_ids": ["batch_parked"]}, status="running")
    await tasks_verify.handle_reverify_open(task_id, {})

    assert taken == [{"rows": [], "parent_id": None, "force": False}]
    assert _urls(taken) == set(), "the stale posting waits for the batch in flight"
