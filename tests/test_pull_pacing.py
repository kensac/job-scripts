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


def test_a_watcher_counts_its_own_pulls_requests_paced_or_not(clock):
    seen: list[str] = []
    client.set_pace({"myworkdayjobs.com": 1})
    with client.on_request(lambda: seen.append("x")):
        client.pace(WALMART)
        client.pace("https://api.lever.co/v0/postings/a")
    client.pace(CITI)
    assert seen == ["x", "x"]


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
        for _ in range(3):
            client.pace("https://api.lever.co/v0/postings/a")

    task = db.query_one("SELECT progress, progress_at FROM tasks WHERE id = %s", (row["id"],))
    assert task is not None and task["progress_at"] is not None
    assert task["progress"]["pull_requests"] == 3
    assert task["progress"]["total"] == 0


def test_a_pull_reports_at_most_once_an_interval(monkeypatch):
    from tasks import ingest

    writes: list[int] = []
    monkeypatch.setattr(ingest, "set_progress", lambda *a, **kw: writes.append(a[1]))
    watch = ingest._PullProgress(1, "wd_walmart")
    for _ in range(500):
        watch()
    assert writes == []
    assert watch.requests == 500
