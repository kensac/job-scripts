"""A resumed classify task must collect the batch it already paid for.

handle_classify_mail re-runs from the top when poll_batches un-parks it. It
selected nothing, because its own claims were in its own payload and the
in-flight guard did not exclude itself, and then returned before reaching
run_batched - so the finished batch was never downloaded and _finish dropped
its ids as provably spent.

Survivable during the backfill, because there was always other mail to select
and the batch got collected as a side effect. With the backfill done, "nothing
else to classify" is the normal state, so this became the normal path: two
batches reached 'completed' at the provider with 0 tokens recorded, and 2,646
messages sat unclassified behind claims nobody would ever collect.

The existing coverage misses it by construction: test_mail_classify.py builds a
SECOND task id to test the cross-task case, so it can never exercise a task
excluding itself.
"""

from __future__ import annotations

import datetime

import pytest

from api import db, mail_store
from api.mail_store import ImportedMessage
from api.tasks import mail_classify


def _msg(f, mid: str = "<r1@x>") -> tuple[int, int]:
    uid = f.make_user()
    mail_store.store_messages(
        uid,
        [
            ImportedMessage(
                provider_message_id=mid,
                source="gmail",
                from_email="recruiter@company.com",
                subject="Interview",
                sent_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
                body_text="Are you free Tuesday?",
            )
        ],
    )
    row = db.query_one("SELECT id FROM email_messages ORDER BY id DESC LIMIT 1")
    assert row is not None
    return row["id"], uid


def _task(claimed: list[int], batch_ids: list[str] | None = None) -> int:
    payload: dict = {"claimed_message_ids": claimed}
    if batch_ids is not None:
        payload["batch_ids"] = batch_ids
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status) "
        "VALUES ('classify_mail', %s, 'running') RETURNING id",
        (db.jsonb(payload),),
    )
    assert row is not None
    return row["id"]


class TestASelfClaimDoesNotHideTheWork:
    def test_a_task_does_not_exclude_its_own_claims(self, f):
        """On resume the task is 'running' and its claims are its own. Without
        excluding itself it selects nothing and there is no work to do."""
        mid, _ = _msg(f)
        tid = _task([mid])
        rows = db.query(
            mail_classify._SELECTION,
            {"cap": 10, "identities": ["nobody@x.com"], "task_id": tid},
        )
        assert [r["id"] for r in rows] == [mid]

    def test_another_tasks_claims_still_hide_the_work(self, f):
        """The guard's actual job: two sweeps must not pay for one message."""
        mid, _ = _msg(f)
        other = _task([mid])
        mine = _task([])
        rows = db.query(
            mail_classify._SELECTION,
            {"cap": 10, "identities": ["nobody@x.com"], "task_id": mine},
        )
        assert rows == []
        assert other != mine


class TestAResumeReachesCollection:
    @pytest.mark.asyncio
    async def test_a_resume_with_nothing_new_still_collects(self, monkeypatch, f):
        """The bug, exactly: no new mail, batches in flight, and the handler
        returned before run_batched. The provider had already been paid."""
        mid, _ = _msg(f)
        tid = _task([mid], batch_ids=["batch_paid_for"])
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
            "VALUES (%s, 'not_job_related', 'high', '{}'::jsonb, 'm')",
            (mid,),
        )
        collected: list[str] = []

        async def fake_run_batched(task_id, shape, specs):
            collected.append("reached")
            return {}, None

        monkeypatch.setattr(mail_classify, "run_batched", fake_run_batched)
        monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
        await mail_classify.handle_classify_mail(tid, {})
        assert collected == ["reached"], "returned before collecting a paid-for batch"

    @pytest.mark.asyncio
    async def test_with_no_batches_and_no_work_it_still_returns_early(self, monkeypatch, f):
        """The early return is right when there is genuinely nothing to do -
        the fix must not turn every idle sweep into a provider call."""
        called: list[str] = []

        async def fake_run_batched(task_id, shape, specs):
            called.append("reached")
            return {}, None

        monkeypatch.setattr(mail_classify, "run_batched", fake_run_batched)
        monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
        await mail_classify.handle_classify_mail(_task([]), {})
        assert called == []

    @pytest.mark.asyncio
    async def test_a_resume_does_not_rewrite_the_claim_ledger(self, monkeypatch, f):
        """run_batched short-circuits to collecting the existing batches and
        never submits the rebuilt specs, so writing them would leave the ledger
        naming messages nobody paid for and un-naming the ones in flight."""
        in_flight, _ = _msg(f, "<inflight@x>")
        fresh, _ = _msg(f, "<fresh@x>")
        tid = _task([in_flight], batch_ids=["batch_paid_for"])

        async def fake_run_batched(task_id, shape, specs):
            return {}, None

        monkeypatch.setattr(mail_classify, "run_batched", fake_run_batched)
        monkeypatch.setattr(mail_classify, "_set_progress", lambda *a, **k: None)
        await mail_classify.handle_classify_mail(tid, {})
        row = db.query_one(
            "SELECT payload -> 'claimed_message_ids' AS ids FROM tasks WHERE id = %s", (tid,)
        )
        assert row is not None
        assert [int(i) for i in row["ids"]] == [in_flight]
        assert fresh


class TestTheCeilingDoesNotStrandPaidWork:
    @pytest.mark.asyncio
    async def test_collection_is_not_refused_when_over_budget(self, monkeypatch, f):
        """The ceiling stops NEW spend. Refusing a resume would discard a batch
        the provider has already been paid for - the same loss by a different
        route, and one I introduced with the ceiling."""
        from api import budget
        from api.tasks import runtime

        tid = _task([], batch_ids=["batch_paid_for"])

        def refuse(projected_usd=None):
            raise budget.FleetBudgetExceeded("over")

        monkeypatch.setattr(runtime.budget, "check_fleet_budget", refuse)

        async def fake_collect(ids, hook):
            return {}

        monkeypatch.setattr("core.batch.collect_batches", fake_collect)
        shape = mail_classify.ONGOING_TASK
        results, _ = await runtime.run_batched(tid, shape, [])
        assert results == {}

    @pytest.mark.asyncio
    async def test_a_fresh_submission_is_still_refused_when_over_budget(self, monkeypatch, f):
        from api import budget
        from api.tasks import runtime

        tid = _task([])

        def refuse(projected_usd=None):
            raise budget.FleetBudgetExceeded("over")

        monkeypatch.setattr(runtime.budget, "check_fleet_budget", refuse)
        with pytest.raises(budget.FleetBudgetExceeded):
            await runtime.run_batched(tid, mail_classify.ONGOING_TASK, [])
