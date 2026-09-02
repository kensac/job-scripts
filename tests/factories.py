"""Builders for the rows a test needs, so a test says what it is about.

Every test in this suite used to open with fifteen lines of INSERT before the
first assertion, which made the setup the loudest part of the file and meant a
schema change touched every test. These return ids and take only the fields the
caller actually cares about; everything else gets a sane default.

Nothing here mocks anything - they write to the real database the suite runs
against, because the behaviour under test is mostly SQL.
"""

from __future__ import annotations

import itertools
from typing import Any

from api import db

_seq = itertools.count(1)


def _next(prefix: str) -> str:
    return f"{prefix}{next(_seq)}"


def make_user(
    *,
    sub: str | None = None,
    email: str | None = None,
    groups: list[str] | None = None,
) -> int:
    sub = sub or _next("sub-")
    email = email or f"{sub}@example.test"
    row = db.query_one(
        """
        INSERT INTO users (sub, email, name, groups) VALUES (%s, %s, %s, %s)
        ON CONFLICT (sub) DO UPDATE SET email = EXCLUDED.email
        RETURNING id
        """,
        (sub, email, "", groups or []),
    )
    assert row is not None
    return row["id"]


def make_source(name: str | None = None, *, active: bool = True) -> str:
    name = name or _next("source-")
    db.execute(
        "INSERT INTO sources (name, listings_url, active) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active",
        (name, f"https://{name}.test/jobs.json", active),
    )
    return name


def make_job(
    *,
    url: str | None = None,
    source: str = "src-test",
    company: str = "Acme",
    title: str = "Engineer",
    active: bool = True,
    uploaded_by: int | None = None,
    comp_min: int | None = None,
    comp_max: int | None = None,
) -> int:
    url = url or f"https://jobs.test/{_next('j')}"
    row = db.query_one(
        """
        INSERT INTO jobs (url, raw_url, source, company, title, active, uploaded_by,
                          comp_min, comp_max)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE SET active = EXCLUDED.active
        RETURNING id
        """,
        (url, url, source, company, title, active, uploaded_by, comp_min, comp_max),
    )
    assert row is not None
    return row["id"]


def make_verdict(
    url: str,
    check_type: str,
    status: str = "passed",
    *,
    prompt_hash: str | None = None,
    content: str | None = None,
    reason: str = "",
) -> None:
    """Append a verdict. Latest row per (url, check_type) wins, so calling this
    twice is how a test expresses "the verdict changed"."""
    from core.store import add_ai_result

    add_ai_result(
        url,
        status,
        reason,
        check_type,
        prompt_hash=prompt_hash,
        input_content=content,
        model="gpt-5-nano",
    )


def make_ready_job(
    *,
    source: str = "src-test",
    content: str = "a long job description " * 20,
    closed: str = "passed",
    clearance: str = "passed",
    **job_kwargs: Any,
) -> tuple[int, str]:
    """A job that has everything the sweeps require: cached content plus a
    decided closed and clearance verdict. This is the state most board and
    filter tests actually want, and building it by hand is where they go wrong.
    """
    job_id = make_job(source=source, **job_kwargs)
    row = db.query_one("SELECT url FROM jobs WHERE id = %s", (job_id,))
    assert row is not None
    url = row["url"]
    make_verdict(url, "content", "passed", content=content, reason="scraped")
    if closed:
        make_verdict(url, "closed", closed)
    if clearance:
        make_verdict(url, "clearance", clearance)
    return job_id, url


def make_filter(
    user_id: int,
    *,
    name: str | None = None,
    prompt: str = "must be a backend role",
    enabled: bool = True,
    on_ambiguous: str = "pass",
) -> dict[str, Any]:
    """Returns the filter row, because tests almost always need prompt_hash -
    it is what verdicts are keyed on."""
    from core.filters import build_custom_instructions, compute_prompt_hash

    name = name or _next("filter-")
    prompt_hash = compute_prompt_hash(build_custom_instructions(prompt, on_ambiguous))
    row = db.query_one(
        """
        INSERT INTO user_filters (user_id, name, prompt, on_ambiguous, fail_closed,
                                  enabled, prompt_hash)
        VALUES (%s, %s, %s, %s, FALSE, %s, %s)
        RETURNING id, name, prompt, on_ambiguous, enabled, prompt_hash
        """,
        (user_id, name, prompt, on_ambiguous, enabled, prompt_hash),
    )
    assert row is not None
    return row


def subscribe(user_id: int, source: str) -> None:
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, source),
    )


def make_board_row(user_id: int, job_id: int, *, status: str | None = None) -> None:
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, job_id) DO UPDATE SET status = EXCLUDED.status",
        (user_id, job_id, status),
    )


def make_task(kind: str, payload: dict[str, Any] | None = None, *, status: str = "pending") -> int:
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES (%s, %s, %s) RETURNING id",
        (kind, db.jsonb(payload or {}), status),
    )
    assert row is not None
    return row["id"]


def make_requirements(
    url: str,
    *,
    has_requirements: bool = True,
    skills_required: list[str] | None = None,
    skills_preferred: list[str] | None = None,
    **fields: Any,
) -> None:
    """A job_requirements row plus its job_skills rows, written the way the
    handler writes them - canonical skill beside the raw text - so a test that
    passes here is testing the same shape production reads."""
    from core import skills as skills_lib

    columns = {
        "yoe_min": None,
        "yoe_max": None,
        "degree_min": None,
        "degree_required": False,
        "degree_fields": [],
        "enrollment_required": False,
        "seniority": None,
        "employment_type": None,
        "clearance": None,
        "citizenship_required": False,
        "sponsorship": None,
        **fields,
    }
    names = ", ".join(columns)
    placeholders = ", ".join(f"%({k})s" for k in columns)
    db.execute(
        f"INSERT INTO job_requirements (url, has_requirements, {names}) "
        f"VALUES (%(url)s, %(has)s, {placeholders})",
        {"url": url, "has": has_requirements, **columns},
    )
    for kind, raws in (("required", skills_required), ("preferred", skills_preferred)):
        for raw in raws or []:
            skill = skills_lib.canonical(raw)
            if skill:
                db.execute(
                    "INSERT INTO job_skills (url, kind, skill, skill_raw) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (url, kind, skill, raw),
                )
