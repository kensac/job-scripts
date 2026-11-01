"""Verdict freshness and failed-line handling across parked batches."""

from __future__ import annotations

import pytest

from api import db
from core import page_fetches
from core.store import add_ai_result
from tasks import runtime as tasks_runtime
from tasks import verify as tasks_verify
from tests.factories import finished, make_batch_result, make_task
from tests.worker_queue_support import (
    _claim_for_test,
    _collect_from,
    _run_batched,
    _submit_ids,
)
from tests.worker_queue_support import _clear_claim as _clear_claim

pytestmark = pytest.mark.usefixtures("_clear_claim")


def _submitted_batch(provider_batch_id: str, minutes_ago: int) -> None:
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, submitted_at) "
        "VALUES (%s, 'reverify', now() - make_interval(mins => %s))",
        (provider_batch_id, minutes_ago),
    )


def _reverify_result(
    task_id: int, url: str, batch_id: str, is_closed: bool, requires_clearance: bool = False
):
    """The sweep asks both axes in one call, the same question the first pass
    asks, so its answer carries both."""
    from core.batch import BatchSpec

    return make_batch_result(
        task_id,
        BatchSpec(
            url,
            "verify",
            "original page",
            "VerifyVerdict",
            {},
            context={"company": "C", "title": "T"},
        ),
        text=(
            f'{{"is_closed": {str(is_closed).lower()}, "closed_reason": "batch fixture", '
            f'"requires_clearance_or_restrictions": {str(requires_clearance).lower()}, '
            f'"clearance_reason": "batch fixture"}}'
        ),
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        model="m",
        batch_id=batch_id,
    )


def test_reverify_does_not_overturn_a_closure_settled_while_it_was_parked():
    """A parked batch carries page text as old as its submission. If something
    closed the job in the meantime, writing our result last would overturn a
    fresh closure with a stale page - and latest-row-wins would put the dead
    posting back on people's boards."""
    url = "https://stale.test/1"
    _submitted_batch("batch_parked", minutes_ago=120)
    # Settled AFTER we submitted, by evidence newer than ours.
    add_ai_result(url, "rejected", "ATS returns gone", "closed")

    task_id = make_task("reverify_chunk", {})
    recorded = tasks_verify._record_reverify_results(
        task_id, [_reverify_result(task_id, url, "batch_parked", is_closed=False)]
    )

    assert recorded == 0
    latest = db.query_one(
        "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert latest["status"] == "rejected", "the fresh closure must still win"
    assert latest["reason"] == "ATS returns gone"


def test_reverify_records_normally_when_nothing_settled_after_submission():
    url = "https://stale.test/2"
    add_ai_result(url, "rejected", "stale closure", "closed")
    # Age the verdict using the DATABASE's clock. add_ai_result stamps
    # created_at from Python while _submitted_batch derives submitted_at from
    # now(), so leaving them a few milliseconds apart made this test compare
    # two clocks - and now() is transaction start time, not statement time, so
    # under a loaded suite the batch could appear to predate the verdict it was
    # meant to follow. The guard then correctly suppressed the write and the
    # test failed for a reason that had nothing to do with the guard.
    db.execute(
        "UPDATE ai_queries SET created_at = now() - interval '1 hour' WHERE url = %s",
        (url,),
    )
    _submitted_batch("batch_fresh", minutes_ago=0)

    task_id = make_task("reverify_chunk", {})
    recorded = tasks_verify._record_reverify_results(
        task_id, [_reverify_result(task_id, url, "batch_fresh", is_closed=False)]
    )

    assert recorded == 1
    latest = db.query_one(
        "SELECT status FROM ai_queries WHERE url = %s AND check_type = 'closed' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert latest["status"] == "passed", "a reverify with newer evidence still reopens"


def test_reverify_records_when_the_evidence_cannot_be_dated():
    """No registry row means no submitted_at to compare against. Recording is
    the pre-existing behaviour and a url is not dropped on a suspicion."""
    url = "https://stale.test/3"
    add_ai_result(url, "rejected", "stale closure", "closed")

    task_id = make_task("reverify_chunk", {})
    recorded = tasks_verify._record_reverify_results(
        task_id, [_reverify_result(task_id, url, "batch_unregistered", is_closed=False)]
    )
    assert recorded == 1


def _verdicts_for(url: str) -> list[tuple[str, str]]:
    return [
        (r["check_type"], r["status"])
        for r in db.query(
            "SELECT check_type, status FROM ai_queries WHERE url = %s "
            "AND check_type IN ('closed', 'clearance') ORDER BY id",
            (url,),
        )
    ]


def _outcomes(task_id: int) -> dict[str, int]:
    from api.ai import batch_results

    return batch_results.outcome_counts(task_id)


def test_reverify_writes_no_verdict_for_a_request_the_provider_failed():
    """A failed line says nothing about the posting. Recording it as written
    would also count it done: progress_counts reads the outcome, so a batch
    that failed every request would report a finished sweep."""
    from core.batch import BatchSpec

    url = "https://failed.test/1"
    task_id = make_task("reverify_chunk", {})
    res = make_batch_result(
        task_id,
        BatchSpec(
            url, "verify", "page", "VerifyVerdict", {}, context={"company": "C", "title": "T"}
        ),
        error="provider refused the request",
        batch_id="batch_failed",
    )

    assert tasks_verify._record_reverify_results(task_id, [res]) == 0
    assert _verdicts_for(url) == []
    assert _outcomes(task_id) == {"failed": 1}


def test_reverify_writes_no_verdict_for_a_request_it_never_sent():
    """The snapshot is the only record of what was asked. A whole-batch
    failure comes back under a custom_id that was never a request, and the
    company and title read off a missing snapshot are an exception that takes
    every other result in the collection down with it."""
    from api.ai import batch_results
    from core.batch import BatchSpec

    url = "https://orphan.test/1"
    task_id = make_task("reverify_chunk", {})
    make_batch_result(
        task_id,
        BatchSpec(
            url, "verify", "page", "VerifyVerdict", {}, context={"company": "C", "title": "T"}
        ),
        text='{"is_closed": true, "closed_reason": "gone", '
        '"requires_clearance_or_restrictions": false, "clearance_reason": ""}',
        batch_id="batch_orphan",
    )
    db.execute("DELETE FROM batch_requests WHERE task_id = %s", (task_id,))
    orphan = next(r for r in batch_results.unconsumed(task_id))
    assert orphan.request is None

    assert tasks_verify._record_reverify_results(task_id, [orphan]) == 0
    assert _verdicts_for(url) == []
    assert _outcomes(task_id) == {"unknown_request": 1}


def test_reverify_replayed_after_a_crash_does_not_write_its_verdicts_twice():
    """Collection is checkpointed before consumption, so a chunk requeued
    between the two replays every result it already wrote. Latest row wins, so
    a double write is not visibly wrong until the usage is counted."""
    url = "https://replay.test/1"
    task_id = make_task("reverify_chunk", {})
    res = _reverify_result(task_id, url, "batch_replay", is_closed=True)

    assert tasks_verify._record_reverify_results(task_id, [res]) == 1
    assert tasks_verify._record_reverify_results(task_id, [res]) == 0
    assert _verdicts_for(url) == [("closed", "rejected"), ("clearance", "passed")]


@pytest.mark.asyncio
async def test_verify_new_defers_to_a_verdict_settled_after_it_submitted(monkeypatch):
    """The request snapshot says which checks were missing when the batch went
    out. Hours later that is a claim about the past: an admin recheck or a
    reverify may have settled one in the meantime, and writing over it with an
    answer read off an older page is the same defect as overturning a closure
    from a parked batch."""
    from core import batch as core_batch

    url = "https://settled.test/1"
    db.execute(
        "INSERT INTO jobs (url, source, company, title, available) "
        "VALUES (%s, 's', 'Acme', 'SWE', true)",
        (url,),
    )
    page_fetches.record(url, "passed", "scraped", "J" * 500)

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        # Both checks were missing at submission, so both were asked for.
        assert all(s.context["needs_closed"] and s.context["needs_clearance"] for s in specs)
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                text=(
                    '{"is_closed": true, "closed_reason": "batch says closed", '
                    '"requires_clearance_or_restrictions": false, "clearance_reason": ""}'
                ),
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
            for s in specs
        }

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("core.batch.submit_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_finished_batches", finished(_collect_from(fake_batch)))

    tid = tasks_runtime.enqueue("verify_new", {"cycle": "t"})
    from api.worker import AwaitingBatch

    _claim_for_test(tid)
    with pytest.raises(AwaitingBatch):
        await tasks_verify.handle_verify_new(tid, {"cycle": "t"})
    # While the batch was parked, something else decided the closed check.
    add_ai_result(url, "passed", "recheck says open", "closed")
    tasks_runtime.resume_parked(tid)
    _claim_for_test(tid)
    await tasks_verify.handle_verify_new(tid, {"cycle": "t"})

    closed = db.query_one(
        "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert (closed["status"], closed["reason"]) == ("passed", "recheck says open")
    assert (
        len(db.query("SELECT 1 FROM ai_queries WHERE url = %s AND check_type = 'closed'", (url,)))
        == 1
    ), "the settled verdict is the only closed row; the batch answer was dropped"
    # The axis nobody settled is still written: the call was paid for either way.
    clearance = db.query_one(
        "SELECT status FROM ai_queries WHERE url = %s AND check_type = 'clearance' "
        "ORDER BY id DESC LIMIT 1",
        (url,),
    )
    assert clearance["status"] == "passed"


@pytest.mark.asyncio
async def test_verify_new_with_no_holes_to_fill_submits_nothing(monkeypatch, f):
    """Every cycle runs this sweep, and most cycles find nothing: a job whose
    closed and clearance checks are both decided is not a request. It says so
    rather than reporting an empty batch, because the queue detectors read the
    label and a run that claims to have verified nothing is not the same thing
    as a run that had nothing to verify."""

    async def refuse(*args, **kwargs):
        raise AssertionError("nothing was missing, so nothing should be submitted")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("core.batch.submit_batches", refuse)
    source = f.make_source("complete-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    f.make_ready_job(source=source)

    tid = tasks_runtime.enqueue("verify_new", {"cycle": "t"})
    _claim_for_test(tid)
    await tasks_verify.handle_verify_new(tid, {"cycle": "t"})
    assert db.query_one("SELECT progress FROM tasks WHERE id = %s", (tid,))["progress"] == {
        "done": 0,
        "total": 0,
        "label": "nothing to verify",
    }


@pytest.mark.asyncio
async def test_verify_new_writes_no_verdict_for_a_line_it_could_not_read(monkeypatch, f):
    """A batch comes back per request, and a request can fail or answer with
    something that is not the schema. Either one must leave the posting exactly
    as unverified as it was, so the next cycle picks it up again; writing a
    verdict off an unreadable line would settle it forever on nothing."""
    from core import batch as core_batch

    # Identical postings are twins (core.near_copy); this is about a different rule.
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('verify_near_copy_reuse', 'false') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )

    source = f.make_source("badline-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    _, broken = f.make_ready_job(source=source, closed="", clearance="")
    _, garbled = f.make_ready_job(source=source, closed="", clearance="")

    async def fake_batch(specs, model, effort, max_out, on_event=None):
        return {
            s.custom_id: core_batch.BatchResult(
                s.custom_id,
                error="provider refused the request" if s.custom_id == broken else None,
                text=None if s.custom_id == broken else "the model apologised instead",
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
            for s in specs
        }

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("core.batch.submit_batches", _submit_ids)
    monkeypatch.setattr("core.batch.collect_finished_batches", finished(_collect_from(fake_batch)))

    tid = tasks_runtime.enqueue("verify_new", {"cycle": "t"})
    await _run_batched(tid, lambda: tasks_verify.handle_verify_new(tid, {"cycle": "t"}))

    assert _verdicts_for(broken) == []
    assert _verdicts_for(garbled) == []
    assert _outcomes(tid) == {"failed": 1, "invalid_output": 1}
    # And nothing is counted done, so the sweep does not report a finished run.
    assert db.query_one("SELECT progress FROM tasks WHERE id = %s", (tid,))["progress"]["done"] == 0
