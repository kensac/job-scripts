"""api.hosts and the claim path around it: a pace per host per address that
the fleet learns from refusals, and a pull that waits without failing."""

from __future__ import annotations

import datetime

import pytest
import requests

from api import db, health, hosts, worker
from tasks import ingest
from tasks.runtime import Deferred


def _config(key, value):
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, db.jsonb(value)),
    )


def _budget(host, egress):
    return db.query_one(
        "SELECT pace_seconds, next_allowed_at, ok, refused FROM host_budget "
        "WHERE host = %s AND egress_group = %s",
        (host, egress),
    )


def test_workday_tenants_share_one_budget_host():
    from core.fetching import forms

    boeing = "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/jobs"
    nvidia = "https://nvidia.wd5.myworkdayjobs.com/en-US/jobs/job/engineer/apply"

    assert hosts.host_of(boeing) == "myworkdayjobs.com"
    assert hosts.host_of(nvidia) == "myworkdayjobs.com"
    assert forms.budget_host(boeing) == "myworkdayjobs.com"
    assert forms.budget_host(nvidia) == "myworkdayjobs.com"
    assert hosts.host_of("https://api.ashbyhq.com/posting-api/job-board/acme") == (
        "api.ashbyhq.com"
    )
    _config("ingest_host_pace_seconds", {"myworkdayjobs.com": 1})
    assert hosts.take(hosts.host_of(boeing), "shared-address") is None
    assert hosts.take(hosts.host_of(nvidia), "shared-address") is not None


def test_a_slot_is_taken_once_per_gap_and_the_gap_learns():
    _config("ingest_host_pace_seconds", {"apply.workable.com": 20})
    # Unpaced host: every take succeeds, nothing waits.
    assert hosts.take("api.ashbyhq.com", "a") is None
    assert hosts.take("api.ashbyhq.com", "a") is None
    # Paced host: the first take opens a 20 s gap; the second waits for it.
    assert hosts.take("apply.workable.com", "a") is None
    opens = hosts.take("apply.workable.com", "a")
    assert opens is not None and opens > datetime.datetime.now(datetime.UTC)
    # Another address has its own clock.
    assert hosts.take("apply.workable.com", "b") is None
    # A refusal doubles the gap and holds the address off; a success narrows
    # it, never below the floor.
    hosts.refused("apply.workable.com", "a")
    assert _budget("apply.workable.com", "a")["pace_seconds"] == 40
    for _ in range(30):
        hosts.succeeded("apply.workable.com", "a")
    b = _budget("apply.workable.com", "a")
    assert b["pace_seconds"] == 20 and b["ok"] == 30 and b["refused"] == 1
    # An unpaced host that gets refused starts at the minimum gap, capped.
    hosts.refused("api.ashbyhq.com", "a")
    assert _budget("api.ashbyhq.com", "a")["pace_seconds"] == hosts.REFUSED_MIN_SECONDS
    for _ in range(10):
        hosts.refused("api.ashbyhq.com", "a")
    assert _budget("api.ashbyhq.com", "a")["pace_seconds"] == hosts.CAP_SECONDS


def test_the_claim_skips_a_throttled_host_and_a_task_that_is_not_due(f):
    f.make_source("wb")
    db.execute(
        "INSERT INTO tasks (kind, payload, status) VALUES ('ingest_source', %s, 'pending')",
        (db.jsonb({"source": "wb", "host": "apply.workable.com"}),),
    )
    db.execute(
        "INSERT INTO host_budget (host, egress_group, pace_seconds, next_allowed_at) "
        "VALUES ('apply.workable.com', %s, 20, now() + interval '1 minute')",
        (hosts.EGRESS_GROUP,),
    )
    assert worker._claim_task() is None
    db.execute("UPDATE host_budget SET next_allowed_at = now() - interval '1 second'")
    claimed = worker._claim_task()
    assert claimed is not None and claimed["payload"]["source"] == "wb"
    db.execute(
        "UPDATE tasks SET status = 'pending', not_before = now() + interval '1 minute' "
        "WHERE id = %s",
        (claimed["id"],),
    )
    assert worker._claim_task() is None
    db.execute(
        "UPDATE tasks SET not_before = now() - interval '1 second' WHERE id = %s", (claimed["id"],)
    )
    assert worker._claim_task() is not None


@pytest.mark.asyncio
async def test_a_deferred_pull_goes_back_to_pending_without_an_attempt(monkeypatch):
    opens = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)

    async def wait(task_id, payload):
        raise Deferred(opens)

    monkeypatch.setitem(worker.HANDLERS, "ingest_source", wait)
    task_id = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('ingest_source', '{\"source\": \"x\"}', 'pending') "
        "RETURNING id"
    )["id"]
    assert await worker.run_once() is True
    row = db.query_one(
        "SELECT status, attempts, not_before, worker FROM tasks WHERE id = %s", (task_id,)
    )
    assert row["status"] == "pending" and row["attempts"] == 0 and row["worker"] is None
    assert abs((row["not_before"] - opens).total_seconds()) < 1


@pytest.mark.asyncio
async def test_a_429_from_the_board_defers_the_pull_and_teaches_the_budget(monkeypatch, f):
    f.make_source("wb")
    db.execute(
        "UPDATE sources SET listings_url = 'https://apply.workable.com/api/v3/accounts/wb/jobs' "
        "WHERE name = 'wb'"
    )
    task_id = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('ingest_source', %s, 'running') RETURNING id",
        (db.jsonb({"source": "wb", "host": "apply.workable.com"}),),
    )["id"]
    resp = requests.Response()
    resp.status_code = 429

    def refuse(url, company):
        raise requests.HTTPError("429 Client Error", response=resp)

    from core.fetching import boards

    monkeypatch.setattr(boards, "fetch_listings", refuse)
    with pytest.raises(Deferred) as excinfo:
        await ingest.handle_ingest_source(task_id, {"source": "wb"})
    b = _budget("apply.workable.com", hosts.EGRESS_GROUP)
    # The seeded floor for Workable is 20 s, so the first refusal doubles it.
    assert b["refused"] == 1 and b["pace_seconds"] == 40
    # This address's slot is closed for its gap, but the pull itself comes
    # back within seconds so an open address can take it.
    assert b["next_allowed_at"] > excinfo.value.not_before
    assert (excinfo.value.not_before - datetime.datetime.now(datetime.UTC)).total_seconds() < 10


def test_an_idle_worker_beside_only_throttled_or_deferred_work_is_not_stalled(f):
    _config("queue_stall_minutes", 1)
    db.execute(
        "INSERT INTO worker_status (name, egress_group, last_seen, current_task_id) "
        "VALUES ('w1', 'w1', now(), NULL)"
    )
    db.execute(
        "INSERT INTO tasks (kind, payload, status, created_at, not_before) VALUES "
        "('ingest_source', %s, 'pending', now() - interval '30 minutes', now() + interval '10 minutes')",
        (db.jsonb({"source": "a", "host": "h1"}),),
    )
    db.execute(
        "INSERT INTO tasks (kind, payload, status, created_at) VALUES "
        "('ingest_source', %s, 'pending', now() - interval '30 minutes')",
        (db.jsonb({"source": "b", "host": "apply.workable.com"}),),
    )
    db.execute(
        "INSERT INTO host_budget (host, egress_group, pace_seconds, next_allowed_at) "
        "VALUES ('apply.workable.com', 'w1', 20, now() + interval '10 minutes')"
    )
    assert not [a for a in health._detect_queue() if a["kind"] == "queue_stalled"]
    db.execute("UPDATE host_budget SET next_allowed_at = now() - interval '1 second'")
    assert [a["subject"] for a in health._detect_queue() if a["kind"] == "queue_stalled"] == ["w1"]


def test_the_admin_view_carries_the_budgets_and_the_pulls_waiting_per_host(client, admin_headers):
    db.execute(
        "INSERT INTO host_budget (host, egress_group, pace_seconds, next_allowed_at, refused) "
        "VALUES ('apply.workable.com', 'hetzner', 40, now() + interval '30 seconds', 2)"
    )
    for name, host, ahead in (
        ("a", "apply.workable.com", 30),
        ("b", "apply.workable.com", 90),
        ("c", "api.lever.co", 10),
    ):
        db.execute(
            "INSERT INTO tasks (kind, payload, status, not_before) VALUES "
            "('ingest_source', %s, 'pending', now() + make_interval(secs => %s))",
            (db.jsonb({"source": name, "host": host}), ahead),
        )
    body = client.get("/v1/admin/host-budgets", headers=admin_headers).json()
    (row,) = body["budgets"]
    assert row["host"] == "apply.workable.com" and row["closed"] is True and row["refused"] == 2
    waiting = {d["host"]: d["count"] for d in body["deferred"]}
    assert waiting == {"apply.workable.com": 2, "api.lever.co": 1}


def test_an_address_refused_with_nothing_accepted_is_blocked_and_alerts():
    assert not hosts.blocked(0, 2) and not hosts.blocked(1, 50) and hosts.blocked(0, 3)
    db.execute(
        "INSERT INTO host_budget (host, egress_group, pace_seconds, ok, refused) VALUES "
        "('apply.workable.com', 'hetzner', 160, 0, 4), ('apply.workable.com', 'oci', 20, 9, 0), "
        "('api.lever.co', 'hetzner', 30, 0, 1)"
    )
    alerts = [a for a in health._detect_silent() if a["kind"] == "address_blocked_by_host"]
    assert [(a["subject"], a["detail"]["addresses"]) for a in alerts] == [
        ("apply.workable.com", ["hetzner"])
    ]
