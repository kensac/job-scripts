from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from api import ai, budget, db, events, metrics, verdicts
from api.budget import Entitlement
from core.filters import build_custom_instructions
from core.store import add_ai_result, get_content, get_custom_result

logger = logging.getLogger("jobtracker_worker")

POLL_SECONDS = float(os.environ.get("JOBTRACKER_WORKER_POLL", "5"))
# Filter runs shard into chunks of this many checks; the shared queue then
# load-balances by availability (fast workers simply claim more chunks).
CHUNK_SIZE = int(os.environ.get("JOBTRACKER_CHUNK_SIZE", "100"))
# In-flight jobs per worker inside a chunk (network time dominates, so calls
# overlap); the adaptive limiter tunes the actual level per host.
MAX_CONCURRENCY = int(os.environ.get("JOBTRACKER_MAX_CONCURRENCY", "6"))
SCRAPE_CONCURRENCY = int(os.environ.get("JOBTRACKER_SCRAPE_CONCURRENCY", "2"))


class AdaptiveLimiter:
    """AIMD concurrency control on a rolling throughput window: grow while the
    completion rate keeps improving, step down when it stalls or errors appear,
    halve on rate limits. Each host converges to its own ceiling."""

    def __init__(self, min_c: int = 1, max_c: int = MAX_CONCURRENCY, window: int = 8):
        self.limit = min(3, max_c)
        self.min_c = min_c
        self.max_c = max_c
        self.window = window
        self._count = 0
        self._errors = 0
        self._win_start = time.monotonic()
        self._prev_rate: Optional[float] = None

    def record(self, error: bool = False, rate_limited: bool = False) -> None:
        if rate_limited:
            self.limit = max(self.min_c, self.limit // 2)
            self._reset()
            return
        if error:
            self._errors += 1
        self._count += 1
        if self._count < self.window:
            return
        elapsed = time.monotonic() - self._win_start
        rate = self._count / elapsed if elapsed > 0 else 0.0
        if self._errors:
            self.limit = max(self.min_c, self.limit - 1)
        elif self._prev_rate is None or rate >= self._prev_rate * 1.05:
            self.limit = min(self.max_c, self.limit + 1)
        elif rate < self._prev_rate * 0.9:
            self.limit = max(self.min_c, self.limit - 1)
        self._prev_rate = rate
        self._reset()

    def _reset(self) -> None:
        self._count = 0
        self._errors = 0
        self._win_start = time.monotonic()
        metrics.WORKER_CONCURRENCY.set(self.limit)
INGEST_INTERVAL_MINUTES = int(os.environ.get("JOBTRACKER_INGEST_INTERVAL_MINUTES", "60"))
# 0 = unlimited. Fixed counts don't scale with users/fleet; per-user budgets
# and fleet throughput are the real controls, these envs are emergency brakes.
INGEST_MAX_AI_PER_SOURCE = int(os.environ.get("JOBTRACKER_INGEST_MAX_AI_PER_SOURCE", "0"))
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
    with db.pool.connection() as conn:
        result = conn.execute(
            f"""
            UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL
            WHERE status = 'running' AND attempts < {MAX_ATTEMPTS}
              AND COALESCE(last_heartbeat, started_at) < now() - interval '{HEARTBEAT_TIMEOUT_MINUTES} minutes'
            """
        )
        if result.rowcount:
            metrics.REAPER_REQUEUES.inc(result.rowcount)
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


def _parent_cancelled(parent_id: int) -> bool:
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (parent_id,))
    return not row or row["status"] == "cancelled"


CHUNK_KINDS = ["run_filter_chunk", "reverify_chunk", "run_filter_batch_chunk"]
# Scheduled runs batch their AI calls through the OpenAI Batch API at half
# price; jobs in one batch chunk (content already cached, so no scraping).
BATCH_CHUNK_SIZE = int(os.environ.get("JOBTRACKER_BATCH_CHUNK_SIZE", "500"))


def _update_parent_progress(parent_id: int) -> None:
    agg = db.query_one(
        "SELECT COALESCE(SUM((progress->>'done')::int), 0) AS done FROM tasks "
        "WHERE kind = ANY(%s) AND (payload->>'parent_id')::bigint = %s",
        (CHUNK_KINDS, parent_id),
    )
    db.execute(
        "UPDATE tasks SET progress = jsonb_set(COALESCE(progress, '{}'::jsonb), "
        "'{done}', to_jsonb(%s::int)), last_heartbeat = now() "
        "WHERE id = %s AND status = 'waiting'",
        (agg["done"] if agg else 0, parent_id),
    )
    events.publish_task(parent_id)


def _materialize_passing(user_id: int) -> int:
    """Mirror of the old write_to_sheet step: every job currently passing ALL
    of the user's enabled filters (and the structural gates) becomes a board
    row. Existing rows (including hidden ones) are untouched, so deleting a
    row means 'bring it back next run if it still passes' while hiding is
    permanent."""
    from api import criteria as crit

    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
    params = {
        "uid": user_id,
        "bypass": settings["bypass_sponsorship_filter"] if settings else True,
        **crit.params(settings),
    }
    with db.pool.connection() as conn:
        result = conn.execute(
            f"""
            WITH enabled AS (
                SELECT prompt_hash FROM user_filters WHERE user_id = %(uid)s AND enabled
            ),
            latest_check AS (
                SELECT DISTINCT ON (url, check_type) url, check_type, status
                FROM ai_queries
                WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')
                ORDER BY url, check_type, id DESC
            ),
            pass_all AS (
                SELECT j.id FROM jobs j
                WHERE (j.source IN (SELECT source FROM user_sources WHERE user_id = %(uid)s)
                       OR j.source = 'sheet_import' OR j.uploaded_by = %(uid)s)
                  AND j.active
                  {crit.SQL}
                  AND EXISTS (SELECT 1 FROM latest_check lc WHERE lc.url = j.url
                              AND lc.check_type = 'closed' AND lc.status = 'passed')
                  AND (%(bypass)s OR EXISTS (SELECT 1 FROM latest_check lc WHERE lc.url = j.url
                              AND lc.check_type = 'clearance' AND lc.status = 'passed'))
                  AND (SELECT COUNT(*) FROM enabled) > 0
                  AND (SELECT COUNT(*) FROM enabled e WHERE (
                        SELECT status FROM ai_queries q WHERE q.url = j.url
                          AND q.check_type = 'custom' AND q.prompt_hash = e.prompt_hash
                          AND q.status IN ('passed', 'rejected')
                        ORDER BY q.id DESC LIMIT 1) = 'passed') = (SELECT COUNT(*) FROM enabled)
            )
            INSERT INTO user_jobs (user_id, job_id)
            SELECT %(uid)s, id FROM pass_all
            ON CONFLICT DO NOTHING
            """,
            params,
        )
        added = result.rowcount
    if added:
        metrics.BOARD_ROWS.labels("materialized").inc(added)
        logger.info(f"Materialized {added} passing jobs onto user {user_id}'s board")
    return added


def _maybe_finalize_parent(parent_id: int) -> None:
    _update_parent_progress(parent_id)
    live = db.query_one(
        "SELECT COUNT(*) AS c FROM tasks WHERE kind = ANY(%s) "
        "AND (payload->>'parent_id')::bigint = %s AND status IN ('pending', 'running')",
        (CHUNK_KINDS, parent_id),
    )
    if live and live["c"]:
        return
    failed = db.query_one(
        "SELECT COUNT(*) AS c FROM tasks WHERE kind = ANY(%s) "
        "AND (payload->>'parent_id')::bigint = %s AND status = 'failed'",
        (CHUNK_KINDS, parent_id),
    )
    parent = db.query_one("SELECT kind, payload FROM tasks WHERE id = %s", (parent_id,))
    if parent and parent["kind"] == "reverify_open":
        try:
            _demote_closed()
        except Exception:
            logger.exception("demotion failed")
    elif parent and (parent["payload"] or {}).get("user_id"):
        try:
            _materialize_passing(parent["payload"]["user_id"])
        except Exception:
            logger.exception("materialize failed")
    n_failed = failed["c"] if failed else 0
    if n_failed:
        db.execute(
            "UPDATE tasks SET status = 'failed', error = %s, finished_at = now() "
            "WHERE id = %s AND status = 'waiting'",
            (f"{n_failed} chunk(s) failed", parent_id),
        )
    else:
        db.execute(
            "UPDATE tasks SET status = 'done', finished_at = now() "
            "WHERE id = %s AND status = 'waiting'",
            (parent_id,),
        )
    events.publish_task(parent_id)


def _reconcile_chunks() -> None:
    db.execute(
        "UPDATE tasks SET status = 'cancelled', error = 'parent cancelled', finished_at = now() "
        "WHERE kind = ANY(%s) AND status = 'pending' "
        "AND (payload->>'parent_id')::bigint IN (SELECT id FROM tasks WHERE status = 'cancelled')",
        (CHUNK_KINDS,),
    )
    for r in db.query(
        """
        SELECT id FROM tasks t WHERE t.status = 'waiting'
        AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.kind = ANY(%s)
            AND (c.payload->>'parent_id')::bigint = t.id
            AND c.status IN ('pending', 'running'))
        """,
        (CHUNK_KINDS,),
    ):
        _maybe_finalize_parent(r["id"])


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

    start = time.monotonic()
    content = await asyncio.to_thread(extract_url_content, url)
    metrics.SCRAPE_DURATION.observe(time.monotonic() - start)
    metrics.SCRAPES.labels("ok" if content else "empty").inc()
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
    """Runs one custom-filter check via the shared verdict primitive; returns
    usage, or None when a cached verdict made the call unnecessary."""
    if get_custom_result(url, prompt_hash, model=cfg.model):
        return None
    _, usage = await verdicts.run_check(
        cfg,
        url=url,
        check_type="custom",
        instructions=instructions,
        input_text=f"Company: {company}\nJob Title: {title}\n\nJob Content:\n{content}",
        response_model=FilterVerdict,
        verdict_of=lambda p: (p.should_filter, p.reason),
        company=company,
        job_title=title,
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        context="filter-run",
    )
    return usage


def _candidates(user_id: int) -> List[Dict[str, Any]]:
    from api import criteria

    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
    return db.query(
        f"""
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
          {criteria.SQL}
          AND EXISTS (SELECT 1 FROM latest_check lc
                      WHERE lc.url = j.url AND lc.check_type = 'closed' AND lc.status = 'passed')
          AND (%(bypass)s
               OR EXISTS (SELECT 1 FROM latest_check lc
                          WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed'))
        ORDER BY j.id DESC
        """,
        {
            "uid": user_id,
            "bypass": settings["bypass_sponsorship_filter"] if settings else True,
            **criteria.params(settings),
        },
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


async def _process_jobs(
    task_id: int,
    user_id: int,
    ent,
    cfg,
    flt: Dict[str, Any],
    jobs: List[Dict[str, Any]],
    parent_id: Optional[int] = None,
) -> None:
    instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
    total = len(jobs)
    done = 0
    limiter = AdaptiveLimiter()
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def one(job: Dict[str, Any]):
        content = get_content(job["url"])
        if not content:
            async with scrape_sem:
                content = await _scrape(job["url"])
        if not content:
            return None
        return await _check_filter(
            cfg, job["url"], job["company"], job["title"], content,
            instructions, flt["prompt_hash"], f"user{user_id}:{flt['name']}",
        )

    idx = 0
    pending: Dict[asyncio.Task, Dict[str, Any]] = {}
    while idx < total or pending:
        while idx < total and len(pending) < limiter.limit:
            pending[asyncio.create_task(one(jobs[idx]))] = jobs[idx]
            idx += 1
        finished, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for t in finished:
            job = pending.pop(t)
            done += 1
            try:
                usage = t.result()
            except Exception as exc:
                # One bad job must not kill the run; the failed verdict is
                # recorded and retried later. Rate limits shrink concurrency.
                s = str(exc).lower()
                limiter.record(error=True, rate_limited="429" in s or "rate limit" in s)
                logger.exception(f"Filter check failed for {job['url']}")
                continue
            limiter.record()
            if usage and usage["total_tokens"]:
                budget.record_usage(
                    user_id, cfg.key_source, "filter", cfg.model,
                    usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"],
                )
            if done % 5 == 0:
                _set_progress(task_id, done, total, flt["name"])
                if parent_id:
                    _update_parent_progress(parent_id)
        if _cancelled(task_id) or (parent_id and _parent_cancelled(parent_id)):
            for t in pending:
                t.cancel()
            logger.info(f"Task {task_id} cancelled mid-run")
            return
        if (
            cfg.key_source == "owner"
            and ent.weekly_token_budget is not None
            and budget.spent_this_week(user_id) >= ent.weekly_token_budget
        ):
            for t in pending:
                t.cancel()
            raise PermissionError(f"BUDGET_EXCEEDED after {done}/{total} checks")
    _set_progress(task_id, total, total, flt["name"])
    if parent_id:
        _update_parent_progress(parent_id)


def _content_ready_urls(urls: List[str]) -> set:
    if not urls:
        return set()
    rows = db.query(
        "SELECT DISTINCT url FROM ai_queries WHERE url = ANY(%s) "
        "AND check_type != 'custom' AND input_content IS NOT NULL AND input_content != ''",
        (urls,),
    )
    return {r["url"] for r in rows}


async def _run_filters(
    task_id: int, user_id: int, filters: List[Dict[str, Any]], batched: bool = False
) -> None:
    """Splitter: compute the undecided work, then shard it. Scheduled (batched)
    runs send content-ready jobs through the half-price Batch API in large
    centralized chunks; jobs still needing a scrape go through live fleet
    chunks as usual (sharded parsing, centralized batching)."""
    ent, cfg = _load_config(user_id)
    candidates = _candidates(user_id)
    urls = [j["url"] for j in candidates]
    use_batch = batched and cfg.key_source == "owner" and cfg.provider == "openai"
    units: List[tuple] = []
    for flt in filters:
        decided = _decided_urls(urls, flt["prompt_hash"], cfg.model)
        todo = [j for j in candidates if j["url"] not in decided]
        metrics.CACHED_VERDICTS.inc(len(candidates) - len(todo))
        if use_batch and todo:
            ready = _content_ready_urls([j["url"] for j in todo])
            batchable = [j for j in todo if j["url"] in ready]
            todo = [j for j in todo if j["url"] not in ready]
            for start in range(0, len(batchable), BATCH_CHUNK_SIZE):
                units.append(("batch", flt, batchable[start : start + BATCH_CHUNK_SIZE]))
        for start in range(0, len(todo), CHUNK_SIZE):
            units.append(("live", flt, todo[start : start + CHUNK_SIZE]))
    if not units:
        _materialize_passing(user_id)
        _set_progress(task_id, 0, 0, "everything already decided")
        return
    if len(units) == 1 and units[0][0] == "live":
        _, flt, jobs = units[0]
        await _process_jobs(task_id, user_id, ent, cfg, flt, jobs)
        _materialize_passing(user_id)
        return
    total = sum(len(jobs) for _, _, jobs in units)
    for mode, flt, jobs in units:
        enqueue(
            "run_filter_batch_chunk" if mode == "batch" else "run_filter_chunk",
            {
                "parent_id": task_id,
                "user_id": user_id,
                "filter": {
                    k: flt[k] for k in ("name", "prompt", "on_ambiguous", "prompt_hash")
                },
                "jobs": jobs,
            },
        )
    db.execute(
        "UPDATE tasks SET status = 'waiting', progress = %s WHERE id = %s AND status = 'running'",
        (
            db.jsonb({"done": 0, "total": total, "label": f"{len(units)} chunks across the fleet"}),
            task_id,
        ),
    )
    events.publish_task(task_id)


async def handle_run_filter_chunk(task_id: int, payload: Dict[str, Any]) -> None:
    ent, cfg = _load_config(payload["user_id"])
    await _process_jobs(
        task_id,
        payload["user_id"],
        ent,
        cfg,
        payload["filter"],
        payload["jobs"],
        parent_id=payload["parent_id"],
    )


async def handle_run_filter_batch_chunk(task_id: int, payload: Dict[str, Any]) -> None:
    """Centralized half-price path: one worker submits the whole chunk to the
    OpenAI Batch API (core/batch.py enforces the enqueued-token budget in
    waves) and records every verdict when results land."""
    import json as _json

    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec, run_responses_batch

    user_id = payload["user_id"]
    flt = payload["filter"]
    jobs = payload["jobs"]
    parent_id = payload["parent_id"]
    ent, cfg = _load_config(user_id)
    if cfg.key_source != "owner" or cfg.provider != "openai":
        # Entitlement changed since split (e.g. BYO key added): run live.
        await _process_jobs(task_id, user_id, ent, cfg, flt, jobs, parent_id=parent_id)
        return
    instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
    schema = to_strict_json_schema(FilterVerdict)
    specs, by_url = [], {}
    for job in jobs:
        content = get_content(job["url"])
        if not content:
            continue
        input_text = (
            f"Company: {job['company']}\nJob Title: {job['title']}\n\nJob Content:\n{content}"
        )
        specs.append(
            BatchSpec(job["url"], instructions, input_text, "FilterVerdict", schema)
        )
        by_url[job["url"]] = (job, input_text)
    total = len(jobs)
    if not specs:
        _set_progress(task_id, total, total, "no content-ready jobs")
        if parent_id:
            _update_parent_progress(parent_id)
        return
    _set_progress(task_id, 0, total, f"batch of {len(specs)} submitted (half price)")
    if parent_id:
        _update_parent_progress(parent_id)

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(60)
            db.execute(
                "UPDATE tasks SET last_heartbeat = now() WHERE id = %s", (task_id,)
            )
            if _cancelled(task_id) or (parent_id and _parent_cancelled(parent_id)):
                raise asyncio.CancelledError

    hb = asyncio.create_task(_heartbeat())
    try:
        results = await run_responses_batch(
            specs, cfg.model, cfg.params.get("reasoning_effort", "medium"), 6000
        )
    finally:
        hb.cancel()
    done = 0
    for url, res in results.items():
        done += 1
        job, input_text = by_url[url]
        usage = {
            "prompt_tokens": (res.usage or {}).get("input_tokens", 0),
            "completion_tokens": (res.usage or {}).get("output_tokens", 0),
            "total_tokens": (res.usage or {}).get("total_tokens", 0),
        }
        if res.error or not res.text:
            add_ai_result(
                url, "failed", f"batch: {res.error or 'no output'}", "custom",
                model=cfg.model, filter_name=f"user{user_id}:{flt['name']}",
                prompt_hash=flt["prompt_hash"], company=job["company"],
                job_title=job["title"], config_name="filter-batch",
                error=res.error,
            )
            metrics.CHECKS.labels("custom", "failed").inc()
            metrics.AI_CALLS.labels(cfg.provider, cfg.model, "error").inc()
            continue
        try:
            parsed = FilterVerdict(**_json.loads(res.text))
        except Exception:
            add_ai_result(
                url, "failed", "batch: unparsable output", "custom",
                model=cfg.model, prompt_hash=flt["prompt_hash"],
                company=job["company"], job_title=job["title"],
                config_name="filter-batch",
            )
            metrics.CHECKS.labels("custom", "failed").inc()
            continue
        verdicts.record_ai_verdict(
            url=url, check_type="custom", rejected=parsed.should_filter,
            reason=parsed.reason, parsed_json=res.text, usage=usage,
            model=cfg.model, provider=cfg.provider, key_source=cfg.key_source,
            company=job["company"], job_title=job["title"],
            instructions=instructions, input_text=input_text,
            filter_name=f"user{user_id}:{flt['name']}", prompt_hash=flt["prompt_hash"],
            context="filter-batch", batched=True,
        )
        if usage["total_tokens"]:
            budget.record_usage(
                user_id, cfg.key_source, "filter", cfg.model,
                usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"],
            )
        if done % 50 == 0:
            _set_progress(task_id, done, total, flt["name"])
            if parent_id:
                _update_parent_progress(parent_id)
    _set_progress(task_id, total, total, flt["name"])
    if parent_id:
        _update_parent_progress(parent_id)


async def handle_run_filter(task_id: int, payload: Dict[str, Any]) -> None:
    flt = db.query_one(
        "SELECT * FROM user_filters WHERE id = %s AND user_id = %s",
        (payload["filter_id"], payload["user_id"]),
    )
    if not flt:
        raise LookupError("unknown filter")
    await _run_filters(task_id, flt["user_id"], [flt], batched=payload.get("batched", False))


async def handle_run_all_filters(task_id: int, payload: Dict[str, Any]) -> None:
    filters = db.query(
        "SELECT * FROM user_filters WHERE user_id = %s AND enabled ORDER BY id",
        (payload["user_id"],),
    )
    if filters:
        await _run_filters(
            task_id, payload["user_id"], filters, batched=payload.get("batched", False)
        )


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
    metrics.INGEST_JOBS.labels(source["name"], "fetched").inc(len(postings))
    metrics.INGEST_JOBS.labels(source["name"], "upserted").inc(upserted)
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
        if INGEST_MAX_AI_PER_SOURCE and checked >= INGEST_MAX_AI_PER_SOURCE:
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
        metrics.INGEST_JOBS.labels(source["name"], "checked").inc()
        if not closed:
            is_closed = await check_if_job_closed(content, p.url, p.title, p.company)
            metrics.CHECKS.labels("closed", "rejected" if is_closed else "passed").inc()
            if is_closed:
                continue
        if not clearance:
            restricted = await check_security_clearance_requirement(
                content, p.url, p.title, p.company
            )
            metrics.CHECKS.labels(
                "clearance", "rejected" if restricted else "passed"
            ).inc()
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
            "AND status IN ('pending', 'running', 'waiting') "
            "AND (payload->>'user_id')::bigint = %s LIMIT 1",
            (u["id"],),
        )
        if active:
            continue
        enqueue(
            "run_all_filters",
            {"user_id": u["id"], "batched": True},
            dedupe_key=f"runall:{u['id']}:{cycle}",
        )


REVERIFY_DAYS = int(os.environ.get("JOBTRACKER_REVERIFY_DAYS", "7"))
REVERIFY_PER_CYCLE = int(os.environ.get("JOBTRACKER_REVERIFY_PER_CYCLE", "0"))  # 0 = all stale

# A board row counts as untouched (machine-managed) when the user never set
# anything on it; only these are auto-added by materialization and auto-removed
# by re-verification.
_UNTOUCHED = """
    (uj.status IS NULL OR uj.status = '') AND uj.date_applied IS NULL
    AND COALESCE(uj.notes, '') = '' AND COALESCE(uj.size, '') = ''
    AND COALESCE(uj.recruiter, '') = '' AND COALESCE(uj.connection1, '') = ''
    AND COALESCE(uj.connection2, '') = '' AND COALESCE(uj.documents, '') = ''
    AND NOT uj.hidden
"""


def _demote_closed() -> int:
    with db.pool.connection() as conn:
        result = conn.execute(
            f"""
            DELETE FROM user_jobs uj USING jobs j
            WHERE uj.job_id = j.id AND {_UNTOUCHED}
              AND (SELECT q.status FROM ai_queries q WHERE q.url = j.url
                   AND q.check_type = 'closed' AND q.status IN ('passed', 'rejected')
                   ORDER BY q.id DESC LIMIT 1) = 'rejected'
            """
        )
        demoted = result.rowcount
    if demoted:
        metrics.BOARD_ROWS.labels("demoted").inc(demoted)
        logger.info(f"Demoted {demoted} closed rows from boards")
    return demoted


async def _reverify_jobs(
    task_id: int, rows: List[Dict[str, Any]], parent_id: Optional[int] = None
) -> None:
    from core import ats
    from core.pittcsc_simplify import (
        CLOSED_INSTRUCTIONS,
        JobClosedResponse,
        extract_url_content,
    )

    key = ai.server_key("openai")
    if not key:
        raise LookupError("no server OpenAI key for reverification")
    cfg = ai.AIConfig(
        provider="openai", api_key=key, key_source="owner",
        model=ai.DEFAULT_MODELS["openai"],
    )
    # Resumability: a requeued chunk (worker died mid-run) skips rows already
    # re-verified in this cycle instead of redoing scrapes and AI calls.
    import datetime as _dt

    cutoff = (_dt.datetime.now() - _dt.timedelta(days=1)).isoformat()
    fresh = {
        r["url"]
        for r in db.query(
            "SELECT DISTINCT url FROM ai_queries WHERE url = ANY(%s) "
            "AND check_type = 'closed' AND created_at > %s",
            ([r["url"] for r in rows], cutoff),
        )
    }
    rows = [r for r in rows if r["url"] not in fresh]
    total = len(rows) + len(fresh)
    done = len(fresh)
    limiter = AdaptiveLimiter()
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def one(r: Dict[str, Any]) -> None:
        ats_res = await asyncio.to_thread(ats.resolve, r["url"])
        if ats_res.status is ats.Status.GONE:
            verdicts.record_manual(
                url=r["url"], check_type="closed", rejected=True,
                reason="ATS reports posting gone (reverify)",
                company=r["company"], job_title=r["title"], context="reverify",
            )
            return
        if ats_res.ok and ats_res.text:
            content = ats_res.text
        else:
            async with scrape_sem:
                content = await asyncio.to_thread(extract_url_content, r["url"])
        if not content:
            return
        await verdicts.run_check(
            cfg,
            url=r["url"],
            check_type="closed",
            instructions=CLOSED_INSTRUCTIONS,
            input_text=content[:60000],
            response_model=JobClosedResponse,
            verdict_of=lambda p: (p.is_closed, p.reason or ""),
            company=r["company"],
            job_title=r["title"],
            context="reverify",
        )

    idx = 0
    n_todo = len(rows)
    pending: Dict[asyncio.Task, Dict[str, Any]] = {}
    while idx < n_todo or pending:
        while idx < n_todo and len(pending) < limiter.limit:
            pending[asyncio.create_task(one(rows[idx]))] = rows[idx]
            idx += 1
        if not pending:
            break
        finished, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for t in finished:
            r = pending.pop(t)
            done += 1
            try:
                t.result()
                limiter.record()
            except Exception as exc:
                s = str(exc).lower()
                limiter.record(error=True, rate_limited="429" in s or "rate limit" in s)
                logger.exception(f"Reverify failed for {r['url']}")
            if done % 5 == 0:
                _set_progress(task_id, done, total, "reverifying open status")
                if parent_id:
                    _update_parent_progress(parent_id)
        if _cancelled(task_id) or (parent_id and _parent_cancelled(parent_id)):
            for t in pending:
                t.cancel()
            return
    _set_progress(task_id, total, total, "reverified")
    if parent_id:
        _update_parent_progress(parent_id)


async def handle_reverify_open(task_id: int, payload: Dict[str, Any]) -> None:
    """Splitter: find every stale untouched board row, shard the re-checks
    across the fleet, demote closed rows when the last chunk lands."""
    cap_sql = "LIMIT %(cap)s" if REVERIFY_PER_CYCLE else ""
    rows = db.query(
        f"""
        SELECT DISTINCT j.url, j.company, j.title FROM user_jobs uj
        JOIN jobs j ON j.id = uj.job_id
        WHERE {_UNTOUCHED}
          AND COALESCE((SELECT MAX(q.created_at) FROM ai_queries q
                        WHERE q.url = j.url AND q.check_type = 'closed'),
                       '1970-01-01')::timestamp < now() - make_interval(days => %(days)s)
        {cap_sql}
        """,
        {"days": REVERIFY_DAYS, "cap": REVERIFY_PER_CYCLE},
    )
    if not rows:
        _set_progress(task_id, 0, 0, "nothing stale")
        _demote_closed()
        return
    if len(rows) <= CHUNK_SIZE:
        await _reverify_jobs(task_id, rows)
        _demote_closed()
        return
    total = len(rows)
    n_chunks = 0
    for start in range(0, total, CHUNK_SIZE):
        enqueue(
            "reverify_chunk",
            {"parent_id": task_id, "rows": rows[start : start + CHUNK_SIZE]},
        )
        n_chunks += 1
    db.execute(
        "UPDATE tasks SET status = 'waiting', progress = %s WHERE id = %s AND status = 'running'",
        (
            db.jsonb({"done": 0, "total": total, "label": f"{n_chunks} chunks across the fleet"}),
            task_id,
        ),
    )
    events.publish_task(task_id)


async def handle_reverify_chunk(task_id: int, payload: Dict[str, Any]) -> None:
    await _reverify_jobs(task_id, payload["rows"], parent_id=payload["parent_id"])


HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
    "run_filter": handle_run_filter,
    "run_all_filters": handle_run_all_filters,
    "run_filter_chunk": handle_run_filter_chunk,
    "run_filter_batch_chunk": handle_run_filter_batch_chunk,
    "ingest_source": handle_ingest_source,
    "reverify_open": handle_reverify_open,
    "reverify_chunk": handle_reverify_chunk,
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
    day = now.strftime("%Y-%m-%d")
    enqueue("reverify_open", {"cycle": day}, dedupe_key=f"reverify:{day}")


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
    task_start = time.monotonic()
    try:
        await handler(task["id"], task["payload"])
        _finish(task["id"], "done")
        metrics.TASKS_PROCESSED.labels(task["kind"], "done").inc()
        logger.info(f"Task {task['id']} done")
    except Exception as exc:
        _finish(task["id"], "failed", str(exc))
        metrics.TASKS_PROCESSED.labels(task["kind"], "failed").inc()
        logger.exception(f"Task {task['id']} failed")
    metrics.TASK_DURATION.labels(task["kind"]).observe(time.monotonic() - task_start)
    if task["kind"] in CHUNK_KINDS:
        try:
            _maybe_finalize_parent(task["payload"]["parent_id"])
        except Exception:
            logger.exception("parent finalize failed")
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
                _reconcile_chunks()
                if ingest_enabled:
                    schedule_ingest_cycle()
            except Exception:
                logger.exception("housekeeping failed")
        worked = asyncio.run(run_once())
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
