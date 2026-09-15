"""The order the new-posting sweep takes its candidates in.

The sweep is capped, which makes its ORDER BY the whole policy: whatever the
cap cuts off waits a cycle, and with a backlog, many cycles. It used to select
in ingest order, so on 2026-09-15 a posting that arrived that morning sat
behind 214,306 older ones and the managed boards, which require this verdict
before a posting is even a candidate, showed nothing new for days.

Seeding a real backlog past the cap is not worth the seconds; the order the
sweep hands its candidates over in is the same decision, observed cheaply.
"""

from __future__ import annotations

import pytest

from api import db
from tasks import verify as tasks_verify


@pytest.fixture
def submitted(monkeypatch):
    """The candidates the sweep asks a provider about, in the order it asks."""
    seen: list[str] = []

    async def fake_run_batched(task_id, task, specs):
        seen.extend(spec.custom_id for spec in specs)
        return [], None

    monkeypatch.setattr(tasks_verify, "run_batched", fake_run_batched)
    return seen


def _post(url: str, days_ago: int | None) -> None:
    db.execute(
        "UPDATE jobs SET date_posted = CASE WHEN %s::int IS NULL THEN NULL "
        "ELSE now() - make_interval(days => %s::int) END WHERE url = %s",
        (days_ago, days_ago, url),
    )


@pytest.mark.asyncio
async def test_the_sweep_takes_the_freshest_postings_first(f, submitted):
    source = f.make_source("verify-order-src")
    uid = f.make_user()
    f.subscribe(uid, source)
    f.make_filter(uid)

    _, old = f.make_ready_job(source=source, closed="", clearance="")
    _, new = f.make_ready_job(source=source, closed="", clearance="")
    _, undated = f.make_ready_job(source=source, closed="", clearance="")
    _post(old, 200)
    _post(new, 1)
    _post(undated, None)

    task_id = f.make_task("verify_new", {}, status="running")
    await tasks_verify.handle_verify_new(task_id, {})

    assert submitted.index(new) < submitted.index(old), (
        "yesterday's posting must not wait behind a backlog months old"
    )
    assert submitted.index(old) < submitted.index(undated), (
        "a posting with no date carries no claim to be fresh"
    )
