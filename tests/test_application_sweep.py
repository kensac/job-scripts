"""Answers ahead of need: the hourly sweep reads the forms of the postings on
a person's board and drafts every missing answer in one batch, so the answer
is there when the posting is opened."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import db
from api.board import visibility
from core.fetching import forms
from tasks import application as drafts
from tests.test_application import GREENHOUSE, _owner_config, _user_id
from tests.test_application import _job_for as _row_for


def _job_for(f, uid: int, url: str) -> int:
    """A posting on the person's board as the sweep sees it: the membership
    the recompute task writes, not only the row the factory inserts."""
    job_id = _row_for(f, uid, url)
    visibility.recompute(uid)
    return job_id


def _sweep_task(uid: int) -> int:
    """A claimed sweep for the person. The one before it is finished first,
    the way the worker finishes a handler, since a sweep steps aside while
    an earlier one for the same person is in flight."""
    db.execute(
        "UPDATE tasks SET status = 'done' WHERE kind = 'application_sweep' "
        "AND status = 'running' AND (payload->>'user_id')::bigint = %s",
        (uid,),
    )
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('application_sweep', %s, 'running') "
        "RETURNING id",
        (db.jsonb({"user_id": uid}),),
    )
    assert row is not None
    return row["id"]


def _fake_batch(monkeypatch, calls, f):
    async def fake_run_batched(task_id, shape, specs, *, charged_to_user=False):
        calls.append([s.custom_id for s in specs])
        return [
            f.make_batch_result(
                task_id,
                s,
                text=json.dumps({"answer": f"Ready: {s.custom_id.partition('|')[2]}."}),
                error=None,
                usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                model="gpt-5.6-luna",
            )
            for s in specs
        ], SimpleNamespace(model="gpt-5.6-luna")

    monkeypatch.setattr(drafts, "run_batched", fake_run_batched)


@pytest.mark.asyncio
async def test_the_sweep_reads_the_board_forms_and_drafts_what_is_missing_once(
    client, user_headers, f, monkeypatch
):
    uid = _user_id()
    on_board = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/10")
    unreadable = _job_for(f, uid, "https://nvidia.wd5.myworkdayjobs.com/en-US/x/job/z")
    # Visible but on nobody's board row: the sweep works the board, not the catalog.
    off_board, _ = f.make_ready_job(url="https://job-boards.greenhouse.io/anthropic/jobs/11")
    client.post("/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers)
    fetched = []
    monkeypatch.setattr(forms, "_get", lambda url: fetched.append(url) or GREENHOUSE)
    _owner_config(monkeypatch)
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls, f)

    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})

    # One read for the readable posting; the Workday one is recorded as
    # unreadable, the off-board one untouched.
    assert len(fetched) == 1 and "anthropic/jobs/10" in fetched[0]
    rows = {r["url"]: r for r in db.query("SELECT url, questions FROM application_forms")}
    assert len(rows["https://job-boards.greenhouse.io/anthropic/jobs/10"]["questions"]) == 2
    assert rows["https://nvidia.wd5.myworkdayjobs.com/en-US/x/job/z"]["questions"] is None
    assert "https://job-boards.greenhouse.io/anthropic/jobs/11" not in rows
    # Only the paragraph question got a row and a draft, in one batch.
    assert calls == [[f"{on_board}|question_1001"]]
    answers = db.query(
        "SELECT job_id, key, draft FROM application_answers WHERE user_id = %s ORDER BY id", (uid,)
    )
    assert [(a["job_id"], a["key"], a["draft"]) for a in answers] == [
        (on_board, "question_1001", "Ready: question_1001.")
    ]
    task = db.query_one("SELECT progress FROM tasks WHERE kind = 'application_sweep'")
    assert task["progress"]["label"].startswith("drafts written ahead of need")

    # The next cycle finds nothing: forms read, drafts written.
    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})
    assert len(fetched) == 1 and calls == [[f"{on_board}|question_1001"]]
    assert unreadable and off_board


@pytest.mark.asyncio
async def test_the_sweep_needs_a_resume_and_respects_the_switch(
    client, user_headers, f, monkeypatch
):
    uid = _user_id()
    _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/12")
    fetched = []
    monkeypatch.setattr(forms, "_get", lambda url: fetched.append(url) or GREENHOUSE)
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls, f)

    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})
    assert fetched == [] and calls == []
    assert (
        db.query_one(
            "SELECT progress FROM tasks WHERE kind = 'application_sweep' ORDER BY id DESC LIMIT 1"
        )["progress"]["label"]
        == "no resume on file"
    )

    client.post("/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers)
    client.put("/v1/user/settings", json={"prefs": {"auto_draft": False}}, headers=user_headers)
    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})
    assert fetched == [] and calls == []

    client.put("/v1/user/settings", json={"prefs": {"auto_draft": True}}, headers=user_headers)
    _owner_config(monkeypatch)
    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})
    assert len(fetched) == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_a_busy_host_is_skipped_this_cycle_not_waited_on(
    client, user_headers, f, monkeypatch
):
    from api import hosts

    uid = _user_id()
    _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/13")
    client.post("/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers)
    fetched = []
    monkeypatch.setattr(forms, "_get", lambda url: fetched.append(url) or GREENHOUSE)
    db.execute(
        "INSERT INTO host_budget (host, egress_group, pace_seconds, next_allowed_at) "
        "VALUES ('boards-api.greenhouse.io', %s, 60, now() + interval '1 minute')",
        (hosts.EGRESS_GROUP,),
    )
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls, f)
    _owner_config(monkeypatch)
    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})
    assert fetched == [] and calls == []
    task = db.query_one("SELECT progress FROM tasks WHERE kind = 'application_sweep'")
    assert "forms: 0 read, 1 host busy" in task["progress"]["label"]


def test_the_cycle_queues_a_sweep_for_each_person_with_a_resume(client, user_headers, f):
    from api import worker

    uid = _user_id()
    client.post("/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers)
    f.make_user(sub="no-resume")
    worker.schedule_ingest_cycle()
    queued = db.query(
        "SELECT payload->>'user_id' AS uid FROM tasks WHERE kind = 'application_sweep'"
    )
    assert [int(r["uid"]) for r in queued] == [uid]
    # Same cycle again queues nothing more.
    worker.schedule_ingest_cycle()
    assert len(db.query("SELECT 1 FROM tasks WHERE kind = 'application_sweep'")) == 1


@pytest.mark.asyncio
async def test_a_sweep_waits_for_an_earlier_one_still_parked_on_its_batch(
    client, user_headers, f, monkeypatch
):
    """The hourly sweep was claimed while a manual one sat parked on its
    batch; both selected the same undrafted rows. The later one steps
    aside; the earlier one is unaffected by the later one's existence."""
    uid = _user_id()
    _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/30")
    client.post("/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers)
    fetched = []
    monkeypatch.setattr(forms, "_get", lambda url: fetched.append(url) or GREENHOUSE)
    _owner_config(monkeypatch)
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls, f)
    parked = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('application_sweep', %s, 'awaiting_batch') "
        "RETURNING id",
        (db.jsonb({"user_id": uid, "batch_ids": ["b1"]}),),
    )
    later = _sweep_task(uid)
    await drafts.handle_application_sweep(later, {"user_id": uid})
    assert fetched == [] and calls == []
    row = db.query_one("SELECT progress FROM tasks WHERE id = %s", (later,))
    assert row["progress"]["label"] == f"sweep {parked['id']} for this person is still in flight"
    # A parked sweep older than the provider's completion window is a stuck
    # task, not a live one: it does not block, or one stuck sweep would be
    # no sweeps forever.
    db.execute(
        "UPDATE tasks SET created_at = now() - interval '25 hours' WHERE id = %s", (parked["id"],)
    )
    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})
    assert len(fetched) == 1 and len(calls) == 1
    db.execute("UPDATE tasks SET created_at = now() WHERE id = %s", (parked["id"],))
    fetched.clear()
    calls.clear()
    db.execute("DELETE FROM application_forms")
    db.execute("DELETE FROM application_answers")
    # Another person's sweep is not in the way.
    other_uid = f.make_user(sub="someone-else")
    db.execute(
        "UPDATE tasks SET payload = %s WHERE id = %s",
        (db.jsonb({"user_id": other_uid}), parked["id"]),
    )
    await drafts.handle_application_sweep(_sweep_task(uid), {"user_id": uid})
    assert len(fetched) == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_a_sweep_back_from_its_batch_collects_and_reads_no_more_forms(
    client, user_headers, f, monkeypatch
):
    """The first sweep read 113 forms on the way out and 114 more on the
    way back from the batch, spending two cycles of reads on one and
    reporting 60 of 158 done. A resumed task only collects."""
    uid = _user_id()
    _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/20")
    client.post("/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers)
    fetched = []
    monkeypatch.setattr(forms, "_get", lambda url: fetched.append(url) or GREENHOUSE)
    _owner_config(monkeypatch)
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls, f)
    task = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('application_sweep', %s, 'running') "
        "RETURNING id",
        (db.jsonb({"user_id": uid, "batch_ids": ["batch_parked"]}),),
    )
    await drafts.handle_application_sweep(
        task["id"], {"user_id": uid, "batch_ids": ["batch_parked"]}
    )
    assert fetched == [] and calls == [[]]
