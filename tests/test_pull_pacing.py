"""The page pace inside a worker (`client.pace`, `hosts.slot_key`) and the
progress a pull reports while it runs (`client.on_request`, ingest's
`_PullProgress`)."""

from __future__ import annotations

import pytest

from api import db
from core.fetching import client
from core.fetching.hosts import pace_key, slot_key

WALMART = "https://walmart.wd504.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs"
WALMART_PAGE_2 = WALMART + "?offset=20"
CITI = "https://citi.wd5.myworkdayjobs.com/wday/cxs/citi/2/jobs"


@pytest.fixture
def clock(monkeypatch):
    """A clock that only moves when the code sleeps, so every wait is exact."""
    now = [1000.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(round(seconds, 3))
        now[0] += seconds

    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client.time, "sleep", sleep)
    monkeypatch.setattr(client, "_last_call", {})
    monkeypatch.setattr(client, "_PACE_SECONDS", {})
    return slept


def test_workday_tenants_share_a_budget_but_not_a_slot():
    assert pace_key(WALMART) == pace_key(CITI) == "myworkdayjobs.com"
    assert slot_key(WALMART) == "walmart.wd504.myworkdayjobs.com"
    assert slot_key(CITI) == "citi.wd5.myworkdayjobs.com"
    assert slot_key(WALMART_PAGE_2) == slot_key(WALMART)


def test_every_other_folded_key_keeps_one_slot():
    northrop = "https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com"
    caci = "https://caci.eightfold.ai/api/pcsx/search?domain=caci.com"
    assert slot_key(northrop) == slot_key(caci) == "eightfold.ai"
    form = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"
    posting = "https://job-boards.greenhouse.io/acme/jobs/1"
    assert slot_key(form) == slot_key(posting) == "boards-api.greenhouse.io"
    assert slot_key("https://api.lever.co/v0/postings/a") == "api.lever.co"


def test_a_workday_tenant_waits_on_its_own_pages_and_not_on_another_tenants(clock):
    client.set_pace({"myworkdayjobs.com": 1})
    client.pace(WALMART)
    client.pace(CITI)
    assert clock == []
    client.pace(WALMART_PAGE_2)
    assert clock == [1.0]


def test_eightfold_tenants_still_take_turns(clock):
    client.set_pace({"eightfold.ai": 2})
    client.pace("https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com")
    client.pace("https://caci.eightfold.ai/api/pcsx/search?domain=caci.com")
    assert clock == [2.0]


def test_a_watcher_sees_its_own_pulls_requests_paced_or_not(clock):
    seen: list[str] = []
    client.set_pace({"myworkdayjobs.com": 1})
    with client.on_request(seen.append):
        client.pace(WALMART, "walmart page 1")
        client.pace("https://api.lever.co/v0/postings/a")
    client.pace(CITI)
    assert seen == ["walmart page 1", "https://api.lever.co/v0/postings/a"]


def test_a_request_is_named_by_its_body_and_parameters_not_its_url_alone():
    from core.fetching.boards.common import _request

    first = _request(WALMART, {"json": {"offset": 0, "limit": 20}})
    again = _request(WALMART, {"json": {"limit": 20, "offset": 0}})
    second = _request(WALMART, {"json": {"offset": 20, "limit": 20}})
    assert first == again
    assert first != second
    assert _request(WALMART, {}) == WALMART


def test_a_pull_reports_its_requests_while_it_runs(monkeypatch):
    """Progress moves during a long pull, so the stall detector reads a pull
    that is working as working."""
    from tasks import ingest

    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status, started_at) "
        "VALUES ('ingest_source', %s, 'running', now()) RETURNING id",
        (db.jsonb({"source": "wd_walmart"}),),
    )
    assert row is not None
    monkeypatch.setattr(ingest, "PULL_PROGRESS_SECONDS", 0.0)
    watch = ingest._PullProgress(row["id"], "wd_walmart")
    with client.on_request(watch):
        for offset in (0, 20, 40):
            client.pace(WALMART, f"offset={offset}")

    task = db.query_one("SELECT progress, progress_at FROM tasks WHERE id = %s", (row["id"],))
    assert task is not None and task["progress_at"] is not None
    assert task["progress"]["pull_requests"] == 3
    assert task["progress"]["total"] == 0


def test_a_pull_asking_the_same_page_again_does_not_advance(monkeypatch):
    """A looping pager must still read as stalled."""
    from tasks import ingest

    writes: list[int] = []
    monkeypatch.setattr(ingest, "PULL_PROGRESS_SECONDS", 0.0)
    monkeypatch.setattr(
        ingest, "checkpoint", lambda tid, done, *a, **kw: writes.append(done) or True
    )
    watch = ingest._PullProgress(1, "wd_walmart")
    for _ in range(5):
        watch("offset=20")
    assert writes == [1, 1, 1, 1, 1]


def test_a_cancelled_task_stops_its_pull(monkeypatch):
    from tasks import ingest

    monkeypatch.setattr(ingest, "PULL_PROGRESS_SECONDS", 0.0)
    monkeypatch.setattr(ingest, "checkpoint", lambda *a, **kw: False)
    with pytest.raises(ingest.PullStopped):
        ingest._PullProgress(1, "wd_walmart")("offset=0")


def test_a_failed_progress_write_never_fails_the_pull(monkeypatch):
    from tasks import ingest

    def broken(*a, **kw):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(ingest, "PULL_PROGRESS_SECONDS", 0.0)
    monkeypatch.setattr(ingest, "checkpoint", broken)
    watch = ingest._PullProgress(1, "wd_walmart")
    watch("offset=0")
    assert watch.requests == 1


@pytest.mark.asyncio
async def test_a_stopped_pull_is_not_a_failed_pull(monkeypatch):
    """Stopping must not reach the failure path, which switches boards off."""
    from core.fetching import boards
    from tasks import ingest

    def stop(*a, **kw):
        raise ingest.PullStopped("wd_walmart")

    switched: list[str] = []
    monkeypatch.setattr(boards, "fetch_listings", stop)
    monkeypatch.setattr(ingest, "_switch_off_if_given_up", lambda s, e: switched.append(s))
    with pytest.raises(ingest.PullStopped):
        await ingest._pull(1, {"name": "wd_walmart", "listings_url": WALMART, "company": "W"}, "h")
    assert switched == []


def test_a_pull_reports_at_most_once_an_interval(monkeypatch):
    from tasks import ingest

    writes: list[int] = []
    monkeypatch.setattr(ingest, "checkpoint", lambda *a, **kw: writes.append(a[1]) or True)
    watch = ingest._PullProgress(1, "wd_walmart")
    for offset in range(500):
        watch(f"offset={offset}")
    assert writes == []
    assert watch.requests == 500
