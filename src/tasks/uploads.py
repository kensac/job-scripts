"""User-submitted URLs: extract a job record from an arbitrary page."""

from __future__ import annotations

from typing import Any

from api import ai, budget, db, verdicts
from core.answers import JobExtract
from core.store import get_content
from tasks.runtime import load_config


async def handle_extract_upload(payload: dict[str, Any]) -> None:
    job = db.query_one("SELECT * FROM jobs WHERE id = %s", (payload["job_id"],))
    if not job:
        raise LookupError("unknown job")
    _, cfg = load_config(payload["user_id"])

    content = None if payload.get("force") else get_content(job["url"])
    if not content:
        content, _closure = await verdicts.refresh_content(
            job["url"],
            company=job.get("company") or "",
            job_title=job.get("title") or "",
            context="upload",
        )
    if not content:
        db.execute("UPDATE jobs SET extraction_status = 'failed' WHERE id = %s", (job["id"],))
        raise RuntimeError("could not extract page content")

    with budget.record_parse_failures(payload["user_id"], cfg.key_source, "extract", cfg.model):
        parsed, usage = await ai.parse(
            cfg,
            (
                "Extract job posting metadata from the page content. "
                "company: employer name. title: role title. locations: list of locations "
                "(empty if remote/unknown). terms: application seasons like 'Summer 2026' "
                "if stated, else empty. Use empty strings/lists when a field is absent."
            ),
            content[:60000],
            JobExtract,
        )
    budget.record_tokens(
        payload["user_id"],
        cfg.key_source,
        "extract",
        cfg.model,
        usage,
    )
    if not parsed:
        db.execute("UPDATE jobs SET extraction_status = 'failed' WHERE id = %s", (job["id"],))
        raise RuntimeError("extraction returned no parsed output")
    db.execute(
        """
        UPDATE jobs SET company = %s, title = %s, locations = %s, terms = %s,
                        extraction_status = 'done'
        WHERE id = %s
        """,
        (parsed.company, parsed.title, parsed.locations, parsed.terms, job["id"]),
    )
