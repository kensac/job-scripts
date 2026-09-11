"""A check is defined once, in the registry, and both routes dispatch it.

The claim this file exists to falsify: that adding a posting check still
requires editing a dispatch ladder. There were two of them, in routers/jobs.py
and routers/admin.py, and they had already drifted apart. If either grows an
arm again, the check registered here stops working through that route.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest
from pydantic import BaseModel, Field

from api import db


class HiringManagerNamedResponse(BaseModel):
    """A check that does not exist in the product, invented by this test."""

    names_a_manager: bool = Field(description="Whether the posting names a hiring manager")
    reason: str | None = None


@pytest.fixture
def registered_check(monkeypatch):
    """A check that exists only for the duration of one test, added the way a
    real one would be: an entry, and nothing else."""
    from core import checks

    spec = checks.PostingCheck(
        name="names_manager",
        instructions="Say whether the posting names a hiring manager.",
        response_model=HiringManagerNamedResponse,
        verdict_of=lambda p: (p.names_a_manager, p.reason or ""),
    )
    monkeypatch.setitem(checks.POSTING_CHECKS, spec.name, spec)
    return spec


def _stub_model(monkeypatch, spec):
    from api import ai
    from api.ai import verdicts

    monkeypatch.setattr(ai, "server_key", lambda provider: "sk-test")

    async def fake_refresh(url, **kw):
        return "A posting body long enough to check.", None

    seen: list[str] = []

    async def fake_parse(cfg, instructions, input_text, response_model):
        seen.append(instructions)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return response_model(names_a_manager=True, reason="names Ada"), usage

    monkeypatch.setattr(verdicts, "refresh_content", fake_refresh)
    monkeypatch.setattr(ai, "parse", fake_parse)
    return seen


def _a_job(url: str) -> int:
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES (%s,'s','C','T') "
        "ON CONFLICT (url) DO NOTHING",
        (url,),
    )
    return db.query_one("SELECT id FROM jobs WHERE url = %s", (url,))["id"]


def test_a_registered_check_runs_through_the_admin_route(
    client, admin_headers, monkeypatch, registered_check
):
    seen = _stub_model(monkeypatch, registered_check)
    job_id = _a_job("https://registry.test/admin")

    r = client.post(
        "/v1/admin/checks/run",
        json={"job_id": job_id, "check": "names_manager"},
        headers=admin_headers,
    )

    assert r.status_code == 200, r.text
    assert seen == [registered_check.instructions], "the entry's own prompt was not used"
    assert r.json()["status"] == "rejected", "verdict_of read the wrong field"
    row = db.query_one(
        "SELECT check_type, reason FROM ai_queries WHERE url = 'https://registry.test/admin' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert row["check_type"] == "names_manager" and row["reason"] == "names Ada"


def test_a_registered_check_runs_through_the_job_route(
    client, user_headers, monkeypatch, registered_check
):
    seen = _stub_model(monkeypatch, registered_check)
    job_id = _a_job("https://registry.test/job")
    # The route serves a job the person can see, and a bare board row is not
    # visible on its own: visibility.FAST admits an untouched row only through
    # board_visible. A status makes it the person's, whatever a verdict says.
    uid = db.query_one("SELECT id FROM users WHERE email = 'user@example.com'")["id"]
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'Interested') "
        "ON CONFLICT (user_id, job_id) DO UPDATE SET status = 'Interested'",
        (uid, job_id),
    )

    r = client.post(
        f"/v1/user/jobs/{job_id}/explain",
        json={"check": "names_manager"},
        headers=user_headers,
    )

    assert r.status_code == 200, r.text
    assert seen == [registered_check.instructions]


def test_the_refusal_names_what_is_registered(client, admin_headers, registered_check):
    """The message is built from the registry, so a new check is offered by it
    rather than being missing from a sentence someone forgot to edit."""
    job_id = _a_job("https://registry.test/bad")
    r = client.post(
        "/v1/admin/checks/run",
        json={"job_id": job_id, "check": "not_a_check"},
        headers=admin_headers,
    )
    assert r.status_code == 400
    assert "names_manager" in r.json()["detail"]["message"]


def test_the_terse_schema_is_the_entry_s_own(registered_check):
    """A check with no cheaper form answers with its full schema either way,
    and one that has a cheaper form only uses it when the reason is not asked
    for. Each caller used to decide this for itself."""
    from core.checks import CLOSED, JobClosedResponse, JobClosedVerdict

    assert CLOSED.model_for(with_reason=True) is JobClosedResponse
    assert CLOSED.model_for(with_reason=False) is JobClosedVerdict
    assert registered_check.terse_model is None
    assert registered_check.model_for(with_reason=False) is HiringManagerNamedResponse
    assert dataclasses.is_dataclass(registered_check)


def test_the_verdict_index_covers_exactly_the_registered_checks():
    """A partial index predicate is a constant in the database, so it cannot
    read the registry. Registering a check without widening the index leaves
    the board query walking rows it used to seek, and nothing says so: the
    answers stay correct and only the plan changes.

    This is the thing that says so. When it fails, the fix is a migration
    widening idx_ai_queries_latest_verdict, in the same pull request as the
    new check.
    """
    import re

    from api.orm import AiQuery
    from core.checks import POSTING_CHECK_NAMES

    index = next(
        ix for ix in AiQuery.__table__.indexes if ix.name == "idx_ai_queries_latest_verdict"
    )
    predicate = str(index.dialect_options["postgresql"]["where"])
    listed = re.search(r"check_type IN \(([^)]*)\)", predicate)
    assert listed, f"the index no longer filters on check_type: {predicate}"
    covered = {name.strip().strip("'") for name in listed.group(1).split(",")}

    assert covered == set(POSTING_CHECK_NAMES), (
        f"idx_ai_queries_latest_verdict covers {sorted(covered)} but the registry holds "
        f"{sorted(POSTING_CHECK_NAMES)}. Widen the index in a migration, or the board "
        "query loses its index for the checks it does not cover."
    )


def test_the_set_of_posting_checks_has_one_definition():
    """The queries that mean "every posting check" read the registry rather
    than spelling the names. A spelled list is one a new check does not join,
    and the failure is silent: the check runs, and the reader ignores it.
    """
    from api.routers.analytics import _VERDICT_CHECKS
    from core.checks import POSTING_CHECK_NAMES
    from core.store import prefetch

    assert _VERDICT_CHECKS is POSTING_CHECK_NAMES
    assert inspect.signature(prefetch).parameters["check_types"].default is POSTING_CHECK_NAMES
