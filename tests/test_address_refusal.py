"""A 403 one address gets while another pulls the same board: the claim, the
ingest decision and the stall detector (tasks.ingest.refused_here)."""

from __future__ import annotations

import pytest
import requests

from api import db, health, hosts, worker
from tasks import ingest
from tasks.runtime import Deferred

ELSEWHERE = "elsewhere"


def _worker(name: str, egress: str) -> None:
    db.execute(
        "INSERT INTO worker_status (name, egress_group, last_seen) VALUES (%s, %s, now())",
        (name, egress),
    )


def _pulled(source: str, by: str, *, days_ago: int = 1) -> None:
    db.execute(
        "INSERT INTO tasks (kind, payload, status, worker, finished_at) "
        "VALUES ('ingest_source', %s, 'done', %s, now() - make_interval(days => %s))",
        (db.jsonb({"source": source}), by, days_ago),
    )


def _running(source: str, refused_by: list[str] | None = None) -> int:
    payload = {"source": source, "host": f"{source}.test"}
    if refused_by is not None:
        payload["refused_by"] = refused_by
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('ingest_source', %s, 'running') "
        "RETURNING id",
        (db.jsonb(payload),),
    )
    assert row is not None
    return row["id"]


def _forbidden(monkeypatch) -> None:
    resp = requests.Response()
    resp.status_code = 403

    def refuse(url, company):
        raise requests.HTTPError("403 Client Error: Forbidden", response=resp)

    from core.fetching import boards

    monkeypatch.setattr(boards, "fetch_listings", refuse)


@pytest.mark.asyncio
async def test_a_403_another_address_does_not_get_goes_to_that_address(monkeypatch, f):
    f.make_source("inova")
    _worker("oci", ELSEWHERE)
    _pulled("inova", "oci")
    task = _running("inova")
    _forbidden(monkeypatch)

    with pytest.raises(Deferred):
        await ingest.handle_ingest_source(task, {"source": "inova"})

    row = db.query_one("SELECT payload FROM tasks WHERE id = %s", (task,))
    assert row is not None and row["payload"]["refused_by"] == [hosts.EGRESS_GROUP]
    active = db.query_one("SELECT active FROM sources WHERE name = 'inova'")
    assert active is not None and active["active"] is True


@pytest.mark.asyncio
async def test_a_403_every_address_gets_is_the_boards_and_fails(monkeypatch, f):
    """No other address has pulled it lately, so nothing says it is up."""
    f.make_source("gone")
    _worker("oci", ELSEWHERE)
    _pulled("gone", "oci", days_ago=31)
    task = _running("gone")
    _forbidden(monkeypatch)

    with pytest.raises(requests.HTTPError):
        await ingest.handle_ingest_source(task, {"source": "gone"})


@pytest.mark.asyncio
async def test_an_address_already_refused_on_this_task_does_not_count_as_elsewhere(monkeypatch, f):
    f.make_source("inova")
    _worker("oci", ELSEWHERE)
    _pulled("inova", "oci")
    task = _running("inova", refused_by=[ELSEWHERE])
    _forbidden(monkeypatch)

    with pytest.raises(requests.HTTPError):
        await ingest.handle_ingest_source(task, {"source": "inova"})


@pytest.mark.asyncio
async def test_this_addresss_own_good_pulls_are_not_elsewhere(monkeypatch, f):
    f.make_source("inova")
    _worker("me", hosts.EGRESS_GROUP)
    _pulled("inova", "me")
    task = _running("inova")
    _forbidden(monkeypatch)

    with pytest.raises(requests.HTTPError):
        await ingest.handle_ingest_source(task, {"source": "inova"})


def test_the_claim_skips_a_pull_this_address_was_refused(f):
    f.make_source("inova")
    task = _running("inova", refused_by=[hosts.EGRESS_GROUP])
    db.execute("UPDATE tasks SET status = 'pending' WHERE id = %s", (task,))
    assert worker._claim_task() is None
    db.execute(
        "UPDATE tasks SET payload = jsonb_set(payload, '{refused_by}', %s) WHERE id = %s",
        (db.jsonb([ELSEWHERE]), task),
    )
    claimed = worker._claim_task()
    assert claimed is not None and claimed["id"] == task


def test_an_idle_worker_beside_a_pull_it_was_refused_is_not_stalled(f):
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('queue_stall_minutes', '1') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    _worker("w1", "w1")
    db.execute(
        "INSERT INTO tasks (kind, payload, status, created_at) VALUES "
        "('ingest_source', %s, 'pending', now() - interval '30 minutes')",
        (db.jsonb({"source": "inova", "host": "inova.test", "refused_by": ["w1"]}),),
    )
    assert not [a for a in health._detect_queue() if a["kind"] == "queue_stalled"]
    db.execute("UPDATE tasks SET payload = payload - 'refused_by'")
    assert [a["subject"] for a in health._detect_queue() if a["kind"] == "queue_stalled"] == ["w1"]


@pytest.mark.asyncio
async def test_an_address_with_no_live_worker_left_is_not_elsewhere(monkeypatch, f):
    """Deferred to an address nobody can claim from, the pull would wait
    forever and no new one would be made while it is pending."""
    f.make_source("inova")
    _worker("oci", ELSEWHERE)
    db.execute("UPDATE worker_status SET last_seen = now() - interval '1 hour' WHERE name = 'oci'")
    _pulled("inova", "oci")
    task = _running("inova")
    _forbidden(monkeypatch)

    with pytest.raises(requests.HTTPError):
        await ingest.handle_ingest_source(task, {"source": "inova"})


@pytest.mark.asyncio
async def test_an_address_that_no_longer_claims_ingest_is_not_elsewhere(monkeypatch, f):
    f.make_source("inova")
    _worker("oci", ELSEWHERE)
    db.execute(
        "UPDATE worker_status SET excluded_kinds = ARRAY['ingest_source'] WHERE name = 'oci'"
    )
    _pulled("inova", "oci")
    task = _running("inova")
    _forbidden(monkeypatch)

    with pytest.raises(requests.HTTPError):
        await ingest.handle_ingest_source(task, {"source": "inova"})


def test_a_task_that_is_no_longer_ours_is_not_written(f):
    f.make_source("inova")
    _worker("oci", ELSEWHERE)
    _pulled("inova", "oci")
    task = _running("inova")
    db.execute("UPDATE tasks SET status = 'pending' WHERE id = %s", (task,))

    with pytest.raises(ingest.PullStopped):
        ingest.refused_here(task, "inova")
    row = db.query_one("SELECT payload FROM tasks WHERE id = %s", (task,))
    assert row is not None and "refused_by" not in row["payload"]
