from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from api import ai, budget, db, metrics
from api.budget import Entitlement
from core.filters import build_custom_instructions
from core.store import add_ai_result, get_content, get_custom_result

logger = logging.getLogger("jobtracker_worker")

POLL_SECONDS = float(os.environ.get("JOBTRACKER_WORKER_POLL", "5"))


class JobExtract(BaseModel):
    company: str
    title: str
    locations: List[str]
    terms: List[str]


class FilterVerdict(BaseModel):
    should_filter: bool
    reason: str


def _claim_task() -> Optional[Dict[str, Any]]:
    return db.query_one(
        """
        UPDATE tasks SET status = 'running', started_at = now()
        WHERE id = (SELECT id FROM tasks WHERE status = 'pending'
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING id, kind, payload
        """
    )


def _finish(task_id: int, status: str, error: Optional[str] = None) -> None:
    db.execute(
        "UPDATE tasks SET status = %s, error = %s, finished_at = now() WHERE id = %s",
        (status, error[:500] if error else None, task_id),
    )


def _load_config(user_id: int) -> tuple[Entitlement, ai.AIConfig]:
    user = db.query_one("SELECT id, sub, email, name, groups FROM users WHERE id = %s", (user_id,))
    if not user:
        raise LookupError("unknown user")
    from api.auth import AuthedUser

    authed = AuthedUser(
        id=user["id"],
        sub=user["sub"],
        email=user["email"] or "",
        name=user["name"] or "",
        groups=user["groups"] or [],
    )
    ent = budget.get_entitlement(authed)
    return ent, budget.resolve_ai_config(user_id, ent)


async def _scrape(url: str) -> Optional[str]:
    from core.pittcsc_simplify import extract_url_content

    content = await asyncio.to_thread(extract_url_content, url)
    if content:
        add_ai_result(url, "passed", "content cached", "content", input_content=content)
    return content


async def handle_extract_upload(payload: Dict[str, Any]) -> None:
    job = db.query_one("SELECT * FROM jobs WHERE id = %s", (payload["job_id"],))
    if not job:
        raise LookupError("unknown job")
    _, cfg = _load_config(payload["user_id"])

    content = None if payload.get("force") else get_content(job["url"])
    content = content or await _scrape(job["url"])
    if not content:
        db.execute(
            "UPDATE jobs SET extraction_status = 'failed' WHERE id = %s", (job["id"],)
        )
        raise RuntimeError("could not extract page content")

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
    if not parsed:
        db.execute(
            "UPDATE jobs SET extraction_status = 'failed' WHERE id = %s", (job["id"],)
        )
        raise RuntimeError("extraction returned no parsed output")

    budget.record_usage(
        payload["user_id"], cfg.key_source, "extract", cfg.model,
        usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"],
    )
    db.execute(
        """
        UPDATE jobs SET company = %s, title = %s, locations = %s, terms = %s,
                        extraction_status = 'done'
        WHERE id = %s
        """,
        (parsed.company, parsed.title, parsed.locations, parsed.terms, job["id"]),
    )


async def _check_filter(
    cfg: ai.AIConfig,
    url: str,
    company: str,
    title: str,
    content: str,
    instructions: str,
    prompt_hash: str,
    filter_name: str,
) -> Optional[Dict[str, int]]:
    """Runs one custom-filter check, records the verdict; returns usage or None if cached."""
    if get_custom_result(url, prompt_hash, model=cfg.model):
        return None
    input_text = f"Company: {company}\nJob Title: {title}\n\nJob Content:\n{content}"
    start = time.monotonic()
    try:
        parsed, usage = await ai.parse(cfg, instructions, input_text, FilterVerdict)
    except Exception as exc:
        add_ai_result(
            url, "failed", f"AI custom filter failed: {str(exc)[:100]}", "custom",
            model=cfg.model, filter_name=filter_name, prompt_hash=prompt_hash,
            company=company, job_title=title, instructions=instructions,
            input_content=input_text, error=str(exc),
        )
        raise
    duration_ms = int((time.monotonic() - start) * 1000)
    if not parsed:
        add_ai_result(
            url, "failed", "AI returned no parsed response", "custom",
            model=cfg.model, filter_name=filter_name, prompt_hash=prompt_hash,
            company=company, job_title=title, instructions=instructions,
            input_content=input_text,
        )
        return usage
    add_ai_result(
        url,
        "rejected" if parsed.should_filter else "passed",
        parsed.reason,
        "custom",
        model=cfg.model,
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        company=company,
        job_title=title,
        instructions=instructions,
        input_content=input_text,
        parsed_json=json.dumps(parsed.model_dump()),
        duration_ms=duration_ms,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
    )
    return usage


def _candidates(user_id: int) -> List[Dict[str, Any]]:
    return db.query(
        """
        WITH latest_check AS (
            SELECT DISTINCT ON (url, check_type) url, check_type, status
            FROM ai_queries
            WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')
            ORDER BY url, check_type, id DESC
        )
        SELECT j.url, j.company, j.title FROM jobs j
        WHERE j.active
          AND (j.source IN (SELECT source FROM user_sources WHERE user_id = %(uid)s)
               OR j.uploaded_by = %(uid)s)
          AND EXISTS (SELECT 1 FROM latest_check lc
                      WHERE lc.url = j.url AND lc.check_type = 'closed' AND lc.status = 'passed')
          AND EXISTS (SELECT 1 FROM latest_check lc
                      WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed')
        ORDER BY j.id DESC
        """,
        {"uid": user_id},
    )


def _set_progress(task_id: int, done: int, total: int, label: str) -> None:
    db.execute(
        "UPDATE tasks SET progress = %s WHERE id = %s",
        (db.jsonb({"done": done, "total": total, "label": label}), task_id),
    )


async def _run_filters(task_id: int, user_id: int, filters: List[Dict[str, Any]]) -> None:
    ent, cfg = _load_config(user_id)
    candidates = _candidates(user_id)
    total = len(candidates) * len(filters)
    done = 0
    for flt in filters:
        instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
        for job in candidates:
            if (
                cfg.key_source == "owner"
                and ent.weekly_token_budget is not None
                and budget.spent_this_week(user_id) >= ent.weekly_token_budget
            ):
                raise PermissionError(f"BUDGET_EXCEEDED after {done}/{total} checks")
            content = get_content(job["url"]) or await _scrape(job["url"])
            done += 1
            if not content:
                continue
            usage = await _check_filter(
                cfg, job["url"], job["company"], job["title"], content,
                instructions, flt["prompt_hash"], f"user{user_id}:{flt['name']}",
            )
            if usage and usage["total_tokens"]:
                budget.record_usage(
                    user_id, cfg.key_source, "filter", cfg.model,
                    usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"],
                )
            if done % 5 == 0 or done == total:
                _set_progress(task_id, done, total, flt["name"])
    _set_progress(task_id, total, total, "")


async def handle_run_filter(task_id: int, payload: Dict[str, Any]) -> None:
    flt = db.query_one(
        "SELECT * FROM user_filters WHERE id = %s AND user_id = %s",
        (payload["filter_id"], payload["user_id"]),
    )
    if not flt:
        raise LookupError("unknown filter")
    await _run_filters(task_id, flt["user_id"], [flt])


async def handle_run_all_filters(task_id: int, payload: Dict[str, Any]) -> None:
    filters = db.query(
        "SELECT * FROM user_filters WHERE user_id = %s AND enabled ORDER BY id",
        (payload["user_id"],),
    )
    if filters:
        await _run_filters(task_id, payload["user_id"], filters)


HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
    "run_filter": handle_run_filter,
    "run_all_filters": handle_run_all_filters,
}


async def run_once() -> bool:
    task = _claim_task()
    if not task:
        return False
    handler = HANDLERS.get(task["kind"])
    logger.info(f"Task {task['id']} ({task['kind']}) starting")
    if not handler:
        _finish(task["id"], "failed", f"unknown task kind: {task['kind']}")
        return True
    try:
        await handler(task["id"], task["payload"])
        _finish(task["id"], "done")
        metrics.TASKS_PROCESSED.labels(task["kind"], "done").inc()
        logger.info(f"Task {task['id']} done")
    except Exception as exc:
        _finish(task["id"], "failed", str(exc))
        metrics.TASKS_PROCESSED.labels(task["kind"], "failed").inc()
        logger.exception(f"Task {task['id']} failed")
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    db.init_schema()
    metrics.serve()
    logger.info("Worker started")
    while True:
        worked = asyncio.run(run_once())
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
