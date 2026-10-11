"""Fetch failures, cancellation, submission, and verdict reuse in a sweep."""

from __future__ import annotations

import asyncio

import pytest

from api import db, fetching
from core.fetching import ats as core_ats
from tasks import reverification
from tasks import runtime as tasks_runtime
from tasks import verify as tasks_verify
from tests.factories import make_batch_result
from tests.reverify_sweep_support import age_closed_verdict


@pytest.fixture
def submitted(monkeypatch):
    """The urls that crossed the submission boundary, per call."""
    calls: list[list[str]] = []

    async def fake_submit(task_id, specs, *args, **kwargs):
        calls.append([spec.custom_id for spec in specs])
        return []

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(core_ats, "resolve", lambda url: core_ats.UNSUPPORTED)
    monkeypatch.setattr(reverification, "submit_or_collect", fake_submit)
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
    age_closed_verdict(url, 30)

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
        "SELECT status, content AS input_content FROM page_fetches WHERE url = %s "
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
        age_closed_verdict(url, 30)

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
        age_closed_verdict(url, 30)
        rows.append({"url": url, "company": "C", "title": "T"})

    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (task_id,))
    await tasks_verify._reverify_jobs(task_id, rows)

    assert tasks_runtime.cancelled(task_id)
    assert submitted == [], "nothing gathered before the stop is paid for after it"
    assert "slow" not in " ".join(completed), "the fetch in flight was cancelled, not awaited"


@pytest.mark.asyncio
async def test_an_unchanged_page_keeps_its_answer_and_a_changed_one_is_asked_again(f, monkeypatch):
    """The sweep exists to notice a page that changed. Asking the same model
    the same question about the same text bought the answer already held in
    5,776 of 5,780 such calls in a week of production. The question travels
    from the paid verdict through the request snapshot to the reuse check, so
    this walks the whole chain: legacy verdict, paid, reused, changed, forced."""
    page = {"text": "a long job description " * 40}

    async def fetch(url):
        return page["text"], False

    model = tasks_verify.resolve(tasks_verify.VERIFY_TASK).model
    submitted: list[list[str]] = []

    async def answer(task_id, specs, *args, **kwargs):
        submitted.append([spec.custom_id for spec in specs])
        return [
            make_batch_result(
                task_id,
                spec,
                text='{"is_closed": false, "closed_reason": "open", '
                '"requires_clearance_or_restrictions": false, "clearance_reason": "none"}',
                model=model,
                usage={"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050},
            )
            for spec in specs
        ]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(core_ats, "resolve", lambda url: core_ats.UNSUPPORTED)
    monkeypatch.setattr(fetching, "fetch_page", fetch)
    monkeypatch.setattr(reverification, "submit_or_collect", answer)
    _, url = f.make_ready_job(source=f.make_source("unchanged-src"))
    rows = [{"url": url, "company": "C", "title": "T"}]

    async def sweep(force: bool = False) -> None:
        age_closed_verdict(url, 30)
        task_id = f.make_task("reverify_chunk", {}, status="running")
        await tasks_verify._reverify_jobs(task_id, rows, force=force)

    await sweep()
    assert submitted == [[url]], "a verdict that recorded no question is asked again"
    fetch = db.query_one(
        "SELECT id FROM page_texts WHERE url = %s ORDER BY id DESC LIMIT 1", (url,)
    )["id"]
    answered = db.query(
        "SELECT page_fetch_id, model_call_id FROM ai_queries "
        "WHERE url = %s AND config_name = 'reverify'",
        (url,),
    )
    assert len(answered) == 2 and {a["page_fetch_id"] for a in answered} == {fetch}
    [call] = {a["model_call_id"] for a in answered}
    assert call is not None, "both axes name the one call that answered them"
    await sweep()
    assert submitted == [[url]], "the same question is not bought twice"
    reused = db.query(
        "SELECT check_type, status, model, model_call_id, request_sha256 FROM ai_queries "
        "WHERE url = %s AND config_name = 'reverify-unchanged' ORDER BY check_type",
        (url,),
    )
    paid = db.query_one(
        "SELECT request_sha256 FROM ai_queries WHERE url = %s AND config_name = 'reverify' "
        "AND check_type = 'closed'",
        (url,),
    )
    assert paid["request_sha256"], "the paid verdict records its question"
    assert reused == [
        {
            "check_type": check,
            "status": "passed",
            "model": None,
            "model_call_id": None,
            "request_sha256": paid["request_sha256"],
        }
        for check in ("clearance", "closed")
    ], "both axes carry the standing answer, and no call is booked for either"

    page["text"] = "this position has been filled " * 40
    await sweep()
    assert submitted == [[url], [url]], "a changed page is a new question"
    await sweep(force=True)
    assert len(submitted) == 3, "a forced sweep doubts the answers and pays"
