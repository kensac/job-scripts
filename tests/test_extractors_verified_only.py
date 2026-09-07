"""Comp and requirements extract only from postings whose closed and
clearance checks both passed: a closed or restricted posting reaches no
board, and nothing reads a number extracted from it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.tasks import comp, requirements


def _refused(specs):
    return {
        s.custom_id: SimpleNamespace(text=None, error="not run", usage=None, batch_id="b")
        for s in specs
    }


def _jobs(f):
    open_id, open_url = f.make_ready_job(url="https://x.test/open")
    closed_id, closed_url = f.make_ready_job(url="https://x.test/closed", closed="rejected")
    restricted_id, restricted_url = f.make_ready_job(
        url="https://x.test/restricted", clearance="rejected"
    )
    unverified_id, unverified_url = f.make_ready_job(
        url="https://x.test/unverified", closed="", clearance=""
    )
    # src-test has no sources row, which AI_ELIGIBLE_JOB admits; the only
    # thing separating the four is their verdicts.
    assert open_id and closed_id and restricted_id and unverified_id
    return open_url, {closed_url, restricted_url, unverified_url}


@pytest.mark.asyncio
async def test_comp_extracts_only_from_verified_open_postings(client, user_headers, f, monkeypatch):
    open_url, others = _jobs(f)
    asked = []

    async def fake(task_id, shape, specs):
        asked.extend(s.custom_id for s in specs)
        return _refused(specs), SimpleNamespace(model="gpt-5-nano")

    monkeypatch.setattr(comp, "run_batched", fake)
    task = f.make_task("extract_comp", status="running")
    await comp.handle_extract_comp(task, {})
    assert asked == [open_url]
    assert not (set(asked) & others)


@pytest.mark.asyncio
async def test_requirements_extracts_only_from_verified_open_postings(
    client, user_headers, f, monkeypatch
):
    open_url, others = _jobs(f)
    asked = []

    async def fake(task_id, shape, specs):
        asked.extend(s.custom_id for s in specs)
        return _refused(specs), SimpleNamespace(model="gpt-5-nano")

    monkeypatch.setattr(requirements, "run_batched", fake)
    task = f.make_task("extract_requirements", status="running")
    await requirements.handle_extract_requirements(task, {})
    assert asked == [open_url]
    assert not (set(asked) & others)
