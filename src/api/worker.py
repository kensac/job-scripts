from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from api import ai, budget, db, events, metrics
from api.budget import Entitlement
from core.filters import build_custom_instructions
from core.store import add_ai_result, get_content, get_custom_result

logger = logging.getLogger("jobtracker_worker")

POLL_SECONDS = float(os.environ.get("JOBTRACKER_WORKER_POLL", "5"))
INGEST_INTERVAL_MINUTES = int(os.environ.get("JOBTRACKER_INGEST_INTERVAL_MINUTES", "60"))
INGEST_MAX_AI_PER_SOURCE = int(os.environ.get("JOBTRACKER_INGEST_MAX_AI_PER_SOURCE", "300"))
# Which task kinds this worker claims; lets small fleet hosts (e.g. an rpi)
# opt out of scrape-heavy work. Default: all kinds.
WORKER_KINDS = [
    k.strip()
    for k in os.environ.get("JOBTRACKER_WORKER_KINDS", "").split(",")
    if k.strip()
]


class JobExtract(BaseModel):
    company: str
    title: str
    locations: List[str]
    terms: List[str]


class FilterVerdict(BaseModel):
    should_filter: bool
    reason: str


MAX_ATTEMPTS = 3
HEARTBEAT_TIMEOUT_MINUTES = 15


def _claim_task() -> Optional[Dict[str, Any]]:
    kinds_clause = "AND kind = ANY(%(kinds)s)" if WORKER_KINDS else ""
    return db.query_one(
        f"""
        UPDATE tasks SET status = 'running', started_at = now(),
                         last_heartbeat = now(), attempts = attempts + 1
        WHERE id = (SELECT id FROM tasks WHERE status = 'pending' {kinds_clause}
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING id, kind, payload
        """,
        {"kinds": WORKER_KINDS},
    )


def reap_stale_tasks() -> None:
    """Recover tasks whose worker died mid-run (deploy, crash, OOM): heartbeat
    goes stale -> requeue up to MAX_ATTEMPTS, then fail permanently."""
    db.execute(
        f"""
        UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL
        WHERE status = 'running' AND attempts < {MAX_ATTEMPTS}
          AND COALESCE(last_heartbeat, started_at) < now() - interval '{HEARTBEAT_TIMEOUT_MINUTES} minutes'
        """
    )
    db.execute(
        f"""
        UPDATE tasks SET status = 'failed', finished_at = now(),
                         error = 'worker lost (heartbeat timeout after ' || attempts || ' attempts)'
        WHERE status = 'running' AND attempts >= {MAX_ATTEMPTS}
          AND COALESCE(last_heartbeat, started_at) < now() - interval '{HEARTBEAT_TIMEOUT_MINUTES} minutes'
        """
    )


def enqueue(kind: str, payload: Dict[str, Any], dedupe_key: Optional[str] = None) -> Optional[int]:
    """Insert a task; with a dedupe_key, at most one task per key ever exists,
    so every fleet worker can race to enqueue and exactly one wins."""
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, dedupe_key) VALUES (%s, %s, %s) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (kind, db.jsonb(payload), dedupe_key),
    )
    if row:
        events.publish_task(row["id"])
    return row["id"] if row else None


def _finish(task_id: int, status: str, error: Optional[str] = None) -> None:
    # Only running tasks can be finished; an admin 'cancelled' status sticks.
    db.execute(
        "UPDATE tasks SET status = %s, error = %s, finished_at = now() "
        "WHERE id = %s AND status = 'running'",
        (status, error[:500] if error else None, task_id),
    )
    events.publish_task(task_id)


def _cancelled(task_id: int) -> bool:
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    return not row or row["status"] != "running"


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
    bypass = db.query_one(
        "SELECT bypass_sponsorship_filter FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
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
          AND (%(bypass)s
               OR EXISTS (SELECT 1 FROM latest_check lc
                          WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed'))
        ORDER BY j.id DESC
        """,
        {"uid": user_id, "bypass": bypass["bypass_sponsorship_filter"] if bypass else True},
    )


def _set_progress(task_id: int, done: int, total: int, label: str) -> None:
    db.execute(
        "UPDATE tasks SET progress = %s, last_heartbeat = now() WHERE id = %s",
        (db.jsonb({"done": done, "total": total, "label": label}), task_id),
    )
    events.publish_task(task_id)


def _decided_urls(urls: List[str], prompt_hash: str, model: str) -> set:
    """URLs that already have a decided verdict for this filter+model - one
    query instead of one per job, so cache-hit reruns cost nothing per row."""
    if not urls:
        return set()
    rows = db.query(
        "SELECT DISTINCT url FROM ai_queries WHERE url = ANY(%s) "
        "AND check_type = 'custom' AND prompt_hash = %s AND model = %s "
        "AND status IN ('passed', 'rejected')",
        (urls, prompt_hash, model),
    )
    return {r["url"] for r in rows}


async def _run_filters(task_id: int, user_id: int, filters: List[Dict[str, Any]]) -> None:
    ent, cfg = _load_config(user_id)
    candidates = _candidates(user_id)
    urls = [j["url"] for j in candidates]
    total = len(candidates) * len(filters)
    done = 0
    checked = 0
    for flt in filters:
        instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
        decided = _decided_urls(urls, flt["prompt_hash"], cfg.model)
        for job in candidates:
            done += 1
            if job["url"] in decided:
                continue
            if checked % 10 == 0:
                if _cancelled(task_id):
                    logger.info(f"Task {task_id} cancelled mid-run")
                    return
                if (
                    cfg.key_source == "owner"
                    and ent.weekly_token_budget is not None
                    and budget.spent_this_week(user_id) >= ent.weekly_token_budget
                ):
                    raise PermissionError(f"BUDGET_EXCEEDED after {done}/{total} checks")
            content = get_content(job["url"]) or await _scrape(job["url"])
            if not content:
                continue
            checked += 1
            try:
                usage = await _check_filter(
                    cfg, job["url"], job["company"], job["title"], content,
                    instructions, flt["prompt_hash"], f"user{user_id}:{flt['name']}",
                )
            except Exception:
                # One bad job (truncated output, transient API error) must not
                # kill the whole run; the failed verdict is recorded and retried
                # on a later pass.
                logger.exception(f"Filter check failed for {job['url']}")
                continue
            if usage and usage["total_tokens"]:
                budget.record_usage(
                    user_id, cfg.key_source, "filter", cfg.model,
                    usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"],
                )
            if checked % 5 == 0:
                _set_progress(task_id, done, total, flt["name"])
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


async def handle_ingest_source(task_id: int, payload: Dict[str, Any]) -> None:
    from core import catalog
    from core.pittcsc_simplify import (
        FALLBACK_CUTOFF_TS,
        check_if_job_closed,
        check_security_clearance_requirement,
        fetch_job_postings,
    )
    from core.store import get_latest, prefetch

    source = db.query_one(
        "SELECT * FROM sources WHERE name = %s AND active", (payload["source"],)
    )
    if not source:
        raise LookupError("unknown or inactive source")

    postings = await asyncio.to_thread(fetch_job_postings, source["listings_url"])
    upserted = catalog.upsert_postings(postings, source["name"])
    logger.info(f"Ingest {source['name']}: fetched {len(postings)}, upserted {upserted}")

    candidates = [
        p for p in postings
        if p.active and p.url and p.date_posted >= FALLBACK_CUTOFF_TS
    ]
    prefetch([p.url for p in candidates])
    checked = 0
    total = len(candidates)
    for i, p in enumerate(candidates):
        if i % 10 == 0 and _cancelled(task_id):
            logger.info(f"Task {task_id} cancelled mid-ingest")
            return
        if checked >= INGEST_MAX_AI_PER_SOURCE:
            logger.info(f"Ingest {source['name']}: AI cap reached ({checked})")
            break
        closed = get_latest(p.url, "closed")
        clearance = get_latest(p.url, "clearance")
        if closed and clearance:
            continue
        content = get_content(p.url) or await _scrape(p.url)
        if not content:
            continue
        checked += 1
        if not closed:
            is_closed = await check_if_job_closed(content, p.url, p.title, p.company)
            if is_closed:
                continue
        if not clearance:
            await check_security_clearance_requirement(content, p.url, p.title, p.company)
        if checked % 5 == 0:
            _set_progress(task_id, i + 1, total, source["name"])
    _set_progress(task_id, total, total, source["name"])

    cycle = payload.get("cycle", "manual")
    users = db.query(
        """
        SELECT DISTINCT u.id FROM users u
        JOIN user_sources us ON us.user_id = u.id
        JOIN user_filters uf ON uf.user_id = u.id AND uf.enabled
        LEFT JOIN user_settings s ON s.user_id = u.id
        WHERE s.api_key_enc IS NOT NULL
           OR u.groups && ARRAY(SELECT group_name FROM group_budgets)::text[]
        """
    )
    for u in users:
        active = db.query_one(
            "SELECT 1 AS x FROM tasks WHERE kind = 'run_all_filters' "
            "AND status IN ('pending', 'running') "
            "AND (payload->>'user_id')::bigint = %s LIMIT 1",
            (u["id"],),
        )
        if active:
            continue
        enqueue(
            "run_all_filters",
            {"user_id": u["id"]},
            dedupe_key=f"runall:{u['id']}:{cycle}",
        )


HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
    "run_filter": handle_run_filter,
    "run_all_filters": handle_run_all_filters,
    "ingest_source": handle_ingest_source,
}


def schedule_ingest_cycle() -> None:
    """Leaderless hourly scheduler: every worker calls this each poll; the
    dedupe key (source + time bucket) guarantees one task per source per cycle
    across the whole fleet."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    bucket = now.replace(
        minute=(now.minute // INGEST_INTERVAL_MINUTES) * INGEST_INTERVAL_MINUTES
        if INGEST_INTERVAL_MINUTES < 60
        else 0,
        second=0,
        microsecond=0,
    )
    cycle = bucket.strftime("%Y-%m-%dT%H:%M")
    for s in db.query("SELECT name FROM sources WHERE active"):
        enqueue(
            "ingest_source",
            {"source": s["name"], "cycle": cycle},
            dedupe_key=f"ingest:{s['name']}:{cycle}",
        )


async def run_once() -> bool:
    task = _claim_task()
    if not task:
        return False
    handler = HANDLERS.get(task["kind"])
    events.publish_task(task["id"])
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
    ingest_enabled = os.environ.get("JOBTRACKER_INGEST_SCHEDULER", "1") == "1"
    logger.info(
        f"Worker started (kinds={WORKER_KINDS or 'all'}, scheduler={'on' if ingest_enabled else 'off'})"
    )
    last_housekeeping = 0.0
    while True:
        if time.monotonic() - last_housekeeping > 60:
            last_housekeeping = time.monotonic()
            try:
                reap_stale_tasks()
                if ingest_enabled:
                    schedule_ingest_cycle()
            except Exception:
                logger.exception("housekeeping failed")
        worked = asyncio.run(run_once())
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
