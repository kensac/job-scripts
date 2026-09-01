from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
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
# Which task kinds this worker claims; lets small fleet hosts (e.g. an rpi)
# opt out of scrape-heavy work. Default: all kinds.
WORKER_KINDS = [
    k.strip()
    for k in os.environ.get("JOBTRACKER_WORKER_KINDS", "").split(",")
    if k.strip()
]
# Stamped on every claimed task so the admin UI can attribute work (and
# failures - e.g. one host's IP getting blocked) to a fleet host. Set it in
# compose; the container hostname fallback is a random hex id.
WORKER_NAME = os.environ.get("JOBTRACKER_WORKER_NAME") or socket.gethostname()


class JobExtract(BaseModel):
    company: str
    title: str
    locations: List[str]
    terms: List[str]


class FilterVerdict(BaseModel):
    should_filter: bool
    reason: str


class FilterVerdictLean(BaseModel):
    """Default verdict shape: no reason text — reasons cost output tokens on
    every call and are only read when a human debugs, so they're generated
    on demand via the explain endpoint instead."""

    should_filter: bool


class JobClosedLean(BaseModel):
    is_closed: bool


class VerifyLean(BaseModel):
    is_closed: bool
    requires_clearance_or_restrictions: bool


MAX_ATTEMPTS = 3
HEARTBEAT_TIMEOUT_MINUTES = 15


def _claim_task() -> Optional[Dict[str, Any]]:
    kinds_clause = "AND kind = ANY(%(kinds)s)" if WORKER_KINDS else ""
    return db.query_one(
        f"""
        UPDATE tasks SET status = 'running', started_at = now(),
                         last_heartbeat = now(), attempts = attempts + 1,
                         worker = %(worker)s
        WHERE id = (SELECT id FROM tasks WHERE status = 'pending' {kinds_clause}
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING id, kind, payload, attempts
        """,
        {"kinds": WORKER_KINDS, "worker": WORKER_NAME},
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
    so every fleet worker can race to enqueue and exactly one wins.

    parent_id is mirrored out of the payload into its own column: it is the
    only payload field that gets queried, and no index can serve
    payload->>'parent_id'. It stays in the payload too so a chunk handler
    reading its own payload is unchanged."""
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, dedupe_key, parent_id) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (kind, db.jsonb(payload), dedupe_key, payload.get("parent_id")),
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
        "WHERE kind = ANY(%s) AND parent_id = %s",
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
                -- DISTINCT for the same reason as _VISIBILITY: duplicate
                -- prompt_hashes cancel out in this query's symmetric counts,
                -- but the two predicates must stay spelled the same way or
                -- the read path and the write path drift apart again.
                SELECT DISTINCT prompt_hash FROM user_filters
                WHERE user_id = %(uid)s AND enabled
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
        "AND parent_id = %s AND status IN ('pending', 'running')",
        (CHUNK_KINDS, parent_id),
    )
    if live and live["c"]:
        return
    failed = db.query_one(
        "SELECT COUNT(*) AS c FROM tasks WHERE kind = ANY(%s) "
        "AND parent_id = %s AND status = 'failed'",
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
        "AND parent_id IN (SELECT id FROM tasks WHERE status = 'cancelled')",
        (CHUNK_KINDS,),
    )
    for r in db.query(
        """
        SELECT id FROM tasks t WHERE t.status = 'waiting'
        AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.kind = ANY(%s)
            AND c.parent_id = t.id
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


async def handle_extract_upload(payload: Dict[str, Any]) -> None:
    job = db.query_one("SELECT * FROM jobs WHERE id = %s", (payload["job_id"],))
    if not job:
        raise LookupError("unknown job")
    _, cfg = _load_config(payload["user_id"])

    content = None if payload.get("force") else get_content(job["url"])
    if not content:
        content, _closure = await verdicts.refresh_content(
            job["url"], company=job.get("company") or "",
            job_title=job.get("title") or "", context="upload",
        )
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
        response_model=FilterVerdictLean,
        verdict_of=lambda p: (p.should_filter, ""),
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
            content, _closure = await verdicts.refresh_content(
                job["url"], company=job.get("company") or "",
                job_title=job.get("title") or "", context="filter-run",
                scrape_sem=scrape_sem,
            )
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
    schema = to_strict_json_schema(FilterVerdictLean)
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
    hook = _batch_event_hook(task_id, "filter", cfg.model)
    try:
        existing = _pending_batch_ids(task_id)
        if existing:
            from core.batch import collect_batches

            logger.info(f"Task {task_id}: reattaching to {len(existing)} in-flight batch(es)")
            results = await collect_batches(existing, hook)
        else:
            results = await run_responses_batch(
                specs, cfg.model, cfg.params.get("reasoning_effort", "medium"), 6000,
                on_event=hook,
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
                error=res.error, batch_id=res.batch_id,
            )
            metrics.CHECKS.labels("custom", "failed").inc()
            metrics.AI_CALLS.labels(cfg.provider, cfg.model, "error").inc()
            continue
        try:
            parsed = FilterVerdictLean(**_json.loads(res.text))
        except Exception:
            add_ai_result(
                url, "failed", "batch: unparsable output", "custom",
                model=cfg.model, prompt_hash=flt["prompt_hash"],
                company=job["company"], job_title=job["title"],
                config_name="filter-batch", batch_id=res.batch_id,
            )
            metrics.CHECKS.labels("custom", "failed").inc()
            continue
        verdicts.record_ai_verdict(
            url=url, check_type="custom", rejected=parsed.should_filter,
            reason="", parsed_json=res.text, usage=usage,
            model=cfg.model, provider=cfg.provider, key_source=cfg.key_source,
            company=job["company"], job_title=job["title"],
            instructions=instructions, input_text=input_text,
            filter_name=f"user{user_id}:{flt['name']}", prompt_hash=flt["prompt_hash"],
            context="filter-batch", batched=True, batch_id=res.batch_id,
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
    from core.pittcsc_simplify import FALLBACK_CUTOFF_TS, fetch_job_postings

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

    # Ingest caches pages but runs NO AI. It reached here through the task
    # queue, which makes it scheduled work, and the rule for scheduled work is
    # that it batches: the hourly verify_new task settles closed+clearance as
    # one half-price call per job. Checking inline here bypassed that — it was
    # why ~90% of closed/clearance verdicts were still full-price sync calls,
    # since verify_new only ever saw the jobs ingest had not already reached.
    #
    # Caching the page stays here on purpose. verify_new can only batch a job
    # whose text is already stored, and leaving that to fetch_missing_content
    # (100/cycle) would throttle verification far below the ingest rate.
    # refresh_content also tags where the text came from, which is what keeps
    # the ats_text_collapse detector fed with live-ingest data.
    candidates = [
        p for p in postings
        if p.active and p.url and p.date_posted >= FALLBACK_CUTOFF_TS
    ]
    # One query to learn which postings already have text, instead of a
    # round trip per posting. The largest source carries ~2,800 active jobs
    # and almost all of them are already cached, so the per-posting form was
    # ~2,800 sequential queries pulling ~15MB to compute a boolean, hourly.
    total = len(candidates)
    have_content = _content_ready_urls([p.url for p in candidates])
    cached = 0
    for i, p in enumerate(candidates):
        if i % 10 == 0 and _cancelled(task_id):
            logger.info(f"Task {task_id} cancelled mid-ingest")
            return
        if p.url in have_content:
            continue
        try:
            await verdicts.refresh_content(
                p.url, company=p.company, job_title=p.title, context="ingest"
            )
        except Exception:
            logger.warning(f"Ingest {source['name']}: content fetch failed for {p.url}")
            continue
        cached += 1
        metrics.INGEST_JOBS.labels(source["name"], "cached").inc()
        if cached % 5 == 0:
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

# Comp extraction runs hourly, so each pass must be bounded. Unbounded it read
# every eligible job in one task: 236 rows today, but 10,665 active jobs still
# need it, which would be ~60MB pulled into one list and ~17 sequential batch
# waves awaited while holding one of three worker slots.
EXTRACT_COMP_PER_CYCLE = int(
    os.environ.get("JOBTRACKER_EXTRACT_COMP_PER_CYCLE", "500")
)

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


def _record_reverify_results(
    results: Dict[str, Any], by_url: Dict[str, Dict[str, Any]], model: str
) -> int:
    """Turns batch lines into closed verdicts. Only parseable lines record —
    anything failed or missing simply stays stale and the next daily sweep
    picks it up, which is what makes the whole task idempotent."""
    recorded = 0
    for url, res in results.items():
        job = by_url.get(url)
        if job is None or res.error or not res.text:
            continue
        try:
            parsed = JobClosedLean.model_validate_json(res.text)
        except Exception:
            continue
        verdicts.record_ai_verdict(
            url=url, check_type="closed", rejected=parsed.is_closed, reason="",
            parsed_json=res.text, model=model,
            usage={
                "prompt_tokens": (res.usage or {}).get("input_tokens", 0),
                "completion_tokens": (res.usage or {}).get("output_tokens", 0),
                "total_tokens": (res.usage or {}).get("total_tokens", 0),
            },
            company=job["company"], job_title=job["title"],
            context="reverify", batched=True, batch_id=res.batch_id,
        )
        recorded += 1
    return recorded


async def _reverify_jobs(
    task_id: int, rows: List[Dict[str, Any]], parent_id: Optional[int] = None,
    force: bool = False,
) -> None:
    """Two phases: gather evidence concurrently (ATS gone-detection, then
    content — the fleet-distributed, network-bound part), then settle every
    remaining verdict in ONE half-price batch instead of a call per job."""
    from core.batch import BatchSpec, collect_batches, run_responses_batch
    from core.pittcsc_simplify import CLOSED_INSTRUCTIONS
    from openai.lib._pydantic import to_strict_json_schema

    if not ai.server_key("openai"):
        raise LookupError("no server OpenAI key for reverification")
    model = ai.DEFAULT_MODELS["openai"]
    by_url = {r["url"]: r for r in rows}
    hook = _batch_event_hook(task_id, "reverify", model)

    # A chunk requeued after submitting reattaches to its live batch instead
    # of rescraping and paying again.
    existing = _pending_batch_ids(task_id)
    if existing:
        logger.info(f"Task {task_id}: reattaching to {len(existing)} reverify batch(es)")
        results = await collect_batches(existing, hook)
        _record_reverify_results(results, by_url, model)
        _set_progress(task_id, len(rows), len(rows), "reverified")
        if parent_id:
            _update_parent_progress(parent_id)
        return

    # Resumability: a requeued chunk skips rows already re-verified this cycle.
    # A forced sweep skips nothing — the point is to overturn existing verdicts.
    import datetime as _dt

    if force:
        fresh: set = set()
        total = len(rows)
        done = 0
    else:
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
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
    needs_ai: List[tuple] = []

    async def gather(r: Dict[str, Any]) -> None:
        content, _closure = await verdicts.refresh_content(
            r["url"], company=r["company"], job_title=r["title"],
            context="reverify", scrape_sem=scrape_sem,
        )
        if not content:
            # Either refresh_content already recorded the closure (ATS gone,
            # or the link bounced to a board index), or the fetch simply
            # failed - which says nothing about the job, so the prior verdict
            # stands and the next cycle retries.
            return
        needs_ai.append((r["url"], content))

    idx = 0
    n_todo = len(rows)
    pending: Dict[asyncio.Task, Dict[str, Any]] = {}
    while idx < n_todo or pending:
        while idx < n_todo and len(pending) < limiter.limit:
            pending[asyncio.create_task(gather(rows[idx]))] = rows[idx]
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
                logger.exception(f"Reverify gather failed for {r['url']}")
            if done % 5 == 0:
                _set_progress(task_id, done, total, "checking open status")
                if parent_id:
                    _update_parent_progress(parent_id)
        if _cancelled(task_id) or (parent_id and _parent_cancelled(parent_id)):
            for t in pending:
                t.cancel()
            return

    if needs_ai:
        _set_progress(task_id, done, total, f"batch of {len(needs_ai)} submitted (half price)")
        if parent_id:
            _update_parent_progress(parent_id)
        schema = to_strict_json_schema(JobClosedLean)
        specs = [
            BatchSpec(url, CLOSED_INSTRUCTIONS, content[:20000], "JobClosedLean", schema)
            for url, content in needs_ai
        ]
        results = await run_responses_batch(specs, model, "low", 1000, on_event=hook)
        _record_reverify_results(results, by_url, model)
    _set_progress(task_id, total, total, "reverified")
    if parent_id:
        _update_parent_progress(parent_id)


async def handle_reverify_open(task_id: int, payload: Dict[str, Any]) -> None:
    """Splitter: find every stale untouched board row, shard the re-checks
    across the fleet, demote closed rows when the last chunk lands.

    full=true re-checks EVERY active job currently believed open, ignoring
    staleness, board membership and the per-cycle cap — for when the evidence
    behind existing verdicts is itself suspect (e.g. verdicts taken before the
    fetcher could tell a redirect from a live page)."""
    if payload.get("full"):
        rows = db.query(
            """
            SELECT j.url, j.company, j.title FROM jobs j
            WHERE j.active AND EXISTS (
                SELECT 1 FROM ai_queries q WHERE q.url = j.url
                  AND q.check_type = 'closed' AND q.status = 'passed')
            ORDER BY j.id
            """
        )
    else:
        cap_sql = "LIMIT %(cap)s" if REVERIFY_PER_CYCLE else ""
        rows = db.query(
            f"""
            SELECT DISTINCT j.url, j.company, j.title FROM user_jobs uj
            JOIN jobs j ON j.id = uj.job_id
            WHERE {_UNTOUCHED}
              AND COALESCE((SELECT MAX(q.created_at) FROM ai_queries q
                            WHERE q.url = j.url AND q.check_type = 'closed'),
                           '-infinity') < now() - make_interval(days => %(days)s)
            {cap_sql}
            """,
            {"days": REVERIFY_DAYS, "cap": REVERIFY_PER_CYCLE},
        )
    if not rows:
        _set_progress(task_id, 0, 0, "nothing stale")
        _demote_closed()
        return
    if len(rows) <= CHUNK_SIZE:
        await _reverify_jobs(task_id, rows, force=bool(payload.get("full")))
        _demote_closed()
        return
    total = len(rows)
    n_chunks = 0
    for start in range(0, total, CHUNK_SIZE):
        enqueue(
            "reverify_chunk",
            {
                "parent_id": task_id,
                "rows": rows[start : start + CHUNK_SIZE],
                "force": bool(payload.get("full")),
            },
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
    await _reverify_jobs(
        task_id, payload["rows"], parent_id=payload["parent_id"],
        force=bool(payload.get("force")),
    )


def _batch_event_hook(task_id: int, purpose: str, model: str):
    """Registers every provider batch in ai_batches as it progresses, and
    stores submitted batch ids on the task payload so a requeued attempt
    reattaches instead of resubmitting (double spend + orphaned results)."""

    def on_event(batch_id: str, status: str, counts: Dict[str, int]) -> None:
        if "input_tokens" in counts or "output_tokens" in counts:
            # Terminal usage report: real token totals -> half-price batch cost.
            inp = counts.get("input_tokens", 0)
            out = counts.get("output_tokens", 0)
            prices = ai.PRICES_PER_MTOK.get(model)
            cost = (
                round((inp * prices[0] + out * prices[1]) / 2_000_000, 6)
                if prices
                else None
            )
            db.execute(
                "UPDATE ai_batches SET input_tokens = input_tokens + %s, "
                "output_tokens = output_tokens + %s, "
                "est_cost_usd = COALESCE(est_cost_usd, 0) + COALESCE(%s, 0), "
                "updated_at = now() WHERE provider_batch_id = %s",
                (inp, out, cost, batch_id),
            )
            events.publish_task(task_id)
            return
        db.execute(
            """
            INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model,
                                    requests, completed, failed_count, status, est_tokens)
            VALUES (%(bid)s, %(tid)s, %(purpose)s, %(model)s,
                    %(requests)s, %(completed)s, %(failed)s, %(status)s, %(est)s)
            ON CONFLICT (provider_batch_id) DO UPDATE SET
                requests = GREATEST(ai_batches.requests, EXCLUDED.requests),
                completed = EXCLUDED.completed,
                failed_count = EXCLUDED.failed_count,
                status = EXCLUDED.status,
                est_tokens = GREATEST(ai_batches.est_tokens, EXCLUDED.est_tokens),
                updated_at = now(),
                completed_at = CASE WHEN EXCLUDED.status IN
                    ('completed', 'failed', 'expired', 'cancelled')
                    THEN COALESCE(ai_batches.completed_at, now()) ELSE NULL END
            """,
            {
                "bid": batch_id, "tid": task_id, "purpose": purpose, "model": model,
                "requests": counts.get("requests", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "status": status,
                "est": counts.get("est_tokens", 0),
            },
        )
        db.execute(
            """
            UPDATE tasks SET payload = jsonb_set(payload, '{batch_ids}',
                COALESCE(payload->'batch_ids', '[]'::jsonb) ||
                CASE WHEN payload->'batch_ids' ? %(bid)s THEN '[]'::jsonb
                     ELSE to_jsonb(%(bid)s::text) END)
            WHERE id = %(tid)s
            """,
            {"bid": batch_id, "tid": task_id},
        )
        events.publish_task(task_id)

    return on_event


def _pending_batch_ids(task_id: int) -> List[str]:
    row = db.query_one("SELECT payload->'batch_ids' AS ids FROM tasks WHERE id = %s", (task_id,))
    return list(row["ids"]) if row and row["ids"] else []


async def handle_send_digests(task_id: int, payload: Dict[str, Any]) -> None:
    """Daily batched digest (never per-event: single-IP mail server, see
    homelab constraints). force+user_id sends the last day's rows regardless
    of digest state — used for template testing by admins."""
    from api import mail
    import secrets as _secrets

    if not mail.configured():
        _set_progress(task_id, 0, 0, "mail not configured")
        return
    force = bool(payload.get("force"))
    where_user = "AND u.id = %(only)s" if payload.get("user_id") else ""
    users = db.query(
        f"""
        SELECT u.id, u.email, s.digest_token, s.last_digest_at
        FROM users u JOIN user_settings s ON s.user_id = u.id
        WHERE (s.email_digest OR %(force)s) AND u.email LIKE '%%@%%' {where_user}
        """,
        {"force": force, "only": payload.get("user_id")},
    )
    sent = 0
    for u in users:
        try:
            since_clause = (
                "uj.created_at > now() - interval '1 day'"
                if force
                else "uj.created_at > COALESCE(%(since)s, now() - interval '1 day')"
            )
            rows = db.query(
                f"""
                SELECT j.company, j.title, j.locations, j.comp_text
                FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
                WHERE uj.user_id = %(uid)s AND {since_clause}
                ORDER BY uj.created_at DESC
                """,
                {"uid": u["id"], "since": u["last_digest_at"]},
            )
            if not rows:
                continue
            token = u["digest_token"]
            if not token:
                token = _secrets.token_urlsafe(24)
                db.execute(
                    "UPDATE user_settings SET digest_token = %s WHERE user_id = %s",
                    (token, u["id"]),
                )
            await asyncio.to_thread(mail.send_digest, u["email"], rows, token)
            if not force:
                db.execute(
                    "UPDATE user_settings SET last_digest_at = now() WHERE user_id = %s",
                    (u["id"],),
                )
            sent += 1
        except Exception:
            logger.exception(f"digest failed for user {u['id']}")
    _set_progress(task_id, sent, len(users), "digests sent")


class CompExtract(BaseModel):
    has_comp: bool
    comp_min: Optional[float] = None
    comp_max: Optional[float] = None
    currency: str = ""
    period: str = ""
    display: str = ""


_COMP_INSTRUCTIONS = (
    "Extract the advertised compensation for THIS job from the page content. "
    "has_comp=true only when a concrete pay amount or range is stated for this role. "
    "comp_min/comp_max: the numeric bounds as decimal numbers exactly as advertised "
    "(26.44 for $26.44/hr, 120000 for $120k/yr; equal when a single amount); "
    "currency: ISO code like USD; period: one of yearly, monthly, hourly; "
    "display: a compact human string exactly as advertised, e.g. '$120k-$150k' or '$45/hr'. "
    "Ignore benefits, equity ranges, and boilerplate salary-law disclaimers without numbers."
)


def _annualize(value: Optional[float], period: str) -> Optional[int]:
    if value is None:
        return None
    if period == "hourly":
        value = value * 2080
    elif period == "monthly":
        value = value * 12
    annual = int(round(value))
    # Model slips (cents-as-ints, stray digits) produce absurd annuals;
    # better no number than a wrong sortable one — display text is kept.
    if annual < 5_000 or annual > 5_000_000:
        return None
    return annual


async def handle_extract_comp(task_id: int, payload: Dict[str, Any]) -> None:
    from core.batch import BatchSpec, run_responses_batch
    from openai.lib._pydantic import to_strict_json_schema

    rows = db.query(
        """
        SELECT j.id, j.url, q.input_content
        FROM jobs j
        JOIN LATERAL (
            SELECT input_content FROM ai_queries q
            WHERE q.url = j.url AND q.input_content IS NOT NULL
              AND length(q.input_content) > 200
            ORDER BY (q.check_type = 'content') DESC, q.id DESC LIMIT 1
        ) q ON TRUE
        WHERE NOT j.comp_extracted AND j.active
        ORDER BY j.id DESC
        LIMIT %(cap)s
        """,
        {"cap": EXTRACT_COMP_PER_CYCLE},
    )
    if not rows:
        _set_progress(task_id, 0, 0, "nothing to extract")
        return
    schema = to_strict_json_schema(CompExtract)
    specs = [
        BatchSpec(r["url"], _COMP_INSTRUCTIONS, r["input_content"][:20000], "CompExtract", schema)
        for r in rows
    ]
    by_url = {r["url"]: r["id"] for r in rows}
    _set_progress(task_id, 0, len(specs), "comp batch submitted (half price)")
    hook = _batch_event_hook(task_id, "comp", ai.DEFAULT_MODELS["openai"])
    existing = _pending_batch_ids(task_id)
    if existing:
        from core.batch import collect_batches

        logger.info(f"Task {task_id}: reattaching to {len(existing)} in-flight batch(es)")
        results = await collect_batches(existing, hook)
    else:
        results = await run_responses_batch(
            specs, ai.DEFAULT_MODELS["openai"], "low", 1500, on_event=hook
        )
    done = 0
    for url, res in results.items():
        job_id = by_url.get(url)
        if job_id is None:
            continue
        comp_min = comp_max = None
        comp_text = None
        parsed_ok = False
        if res.text and not res.error:
            try:
                parsed = CompExtract.model_validate_json(res.text)
                parsed_ok = True
                if parsed.has_comp:
                    comp_min = _annualize(parsed.comp_min, parsed.period)
                    comp_max = _annualize(parsed.comp_max, parsed.period) or comp_min
                    if comp_min and comp_max and comp_min > comp_max:
                        comp_min, comp_max = comp_max, comp_min
                    comp_text = parsed.display or None
            except Exception:
                logger.warning(f"comp parse failed for {url}")
        if parsed_ok:
            db.execute(
                "UPDATE jobs SET comp_min = %s, comp_max = %s, comp_text = %s, "
                "comp_extracted = TRUE WHERE id = %s",
                (comp_min, comp_max, comp_text, job_id),
            )
        # Failed/errored lines stay comp_extracted=false so the next daily
        # sweep retries them — batch operations are idempotent by re-sweep.
        done += 1
        if done % 200 == 0:
            _set_progress(task_id, done, len(specs), "comp extracted")
    _set_progress(task_id, done, len(specs), "comp extracted")


async def handle_data_health(task_id: int, payload: Dict[str, Any]) -> None:
    """Watches for upstream changes that would otherwise surface as a pile of
    quietly misclassified jobs weeks later. Alerts fire once per condition and
    auto-resolve, so the mail stays worth reading."""
    from api import health, mail

    found = health.detect()
    fresh = health.record(found)
    metrics.HEALTH_ALERTS.set(len(found))
    if fresh and mail.configured():
        admins = db.query(
            "SELECT DISTINCT email FROM users WHERE email LIKE '%%@%%' "
            "AND 'infra-admins' = ANY(groups)"
        )
        for a in admins:
            try:
                await asyncio.to_thread(mail.send_health_alert, a["email"], fresh)
            except Exception:
                logger.exception("health alert mail failed")
    _set_progress(
        task_id, len(found), len(found),
        f"{len(found)} open, {len(fresh)} new" if found else "all clear",
    )


CONTENT_BACKFILL_PER_CYCLE = int(
    os.environ.get("JOBTRACKER_CONTENT_BACKFILL_PER_CYCLE", "100")
)


async def handle_fetch_missing_content(task_id: int, payload: Dict[str, Any]) -> None:
    """Jobs nobody ever scraped are invisible to every AI check — they can't be
    verified, filtered, or comp-extracted. This walks that backlog newest-first
    and caches their pages; the existing sweeps then pick them up for free.
    Self-limiting: once every job has content it finds nothing and costs
    nothing."""

    cap = max(1, payload.get("limit") or CONTENT_BACKFILL_PER_CYCLE)
    rows = db.query(
        """
        SELECT j.url, j.company, j.title FROM jobs j
        WHERE j.active AND j.source IN (SELECT source FROM user_sources)
          AND NOT EXISTS (
            SELECT 1 FROM ai_queries q WHERE q.url = j.url
              AND q.input_content IS NOT NULL AND length(q.input_content) > 200)
        ORDER BY j.date_posted DESC NULLS LAST
        LIMIT %s
        """,
        (cap,),
    )
    if not rows:
        _set_progress(task_id, 0, 0, "no content gaps")
        return
    total = len(rows)
    done = fetched = 0
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def one(r: Dict[str, Any]) -> bool:
        content, _closure = await verdicts.refresh_content(
            r["url"], company=r["company"], job_title=r["title"],
            context="content-backfill", scrape_sem=scrape_sem,
        )
        return bool(content)

    limiter = AdaptiveLimiter()
    idx = 0
    pending: Dict[asyncio.Task, Dict[str, Any]] = {}
    while idx < total or pending:
        while idx < total and len(pending) < limiter.limit:
            pending[asyncio.create_task(one(rows[idx]))] = rows[idx]
            idx += 1
        if not pending:
            break
        finished, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for tk in finished:
            r = pending.pop(tk)
            done += 1
            try:
                if tk.result():
                    fetched += 1
                limiter.record()
            except Exception:
                limiter.record(error=True)
                logger.warning(f"content backfill failed for {r['url']}")
            if done % 10 == 0:
                _set_progress(task_id, done, total, f"fetched {fetched} pages")
        if _cancelled(task_id):
            for tk in pending:
                tk.cancel()
            return
    _set_progress(task_id, total, total, f"cached {fetched} of {total} pages")


_VERIFY_INSTRUCTIONS = (
    "Evaluate this job posting on two independent axes.\n"
    "is_closed: true ONLY on posting-specific signals (no longer available/accepting, "
    "position filled, expired, deadline passed, job not found, 404). Site-wide errors, "
    "captchas, access blocks, or login walls say nothing about the job: false. "
    "Ambiguous: false.\n"
    "requires_clearance_or_restrictions: true ONLY for explicit restrictions — required "
    "security clearance or citizenship (US citizen required, US Person, Secret/TS-SCI/"
    "Public Trust), explicit no-sponsorship ('will not sponsor', 'no H1B'), or F1-not-"
    "eligible. Do NOT flag preferences, sponsorship offered, or application questions. "
    "When in doubt: false."
)


async def handle_verify_new(task_id: int, payload: Dict[str, Any]) -> None:
    """Batched replacement for ingest-time closed/clearance checks: one
    half-price call per job yields both verdicts. Idempotent by re-sweep —
    only successful lines produce verdict rows; anything missed or failed is
    picked up by the next cycle's sweep."""
    from core.batch import BatchSpec, run_responses_batch
    from core.store import add_ai_result
    from openai.lib._pydantic import to_strict_json_schema

    rows = db.query(
        """
        SELECT j.url, j.company, j.title, q.input_content,
               NOT EXISTS (
                   SELECT 1 FROM ai_queries c WHERE c.url = j.url
                     AND c.check_type = 'closed'
                     AND c.status IN ('passed', 'rejected')) AS needs_closed,
               NOT EXISTS (
                   SELECT 1 FROM ai_queries c WHERE c.url = j.url
                     AND c.check_type = 'clearance'
                     AND c.status IN ('passed', 'rejected')) AS needs_clearance
        FROM jobs j
        JOIN LATERAL (
            SELECT input_content FROM ai_queries q
            WHERE q.url = j.url AND q.input_content IS NOT NULL
              AND length(q.input_content) > 200
            ORDER BY (q.check_type = 'content') DESC, q.id DESC LIMIT 1
        ) q ON TRUE
        WHERE j.active AND (
            NOT EXISTS (
                SELECT 1 FROM ai_queries c WHERE c.url = j.url
                  AND c.check_type = 'closed' AND c.status IN ('passed', 'rejected'))
            -- Short-circuited pipelines (and any upstream verdict that later
            -- flips to passing) leave downstream checks MISSING, not false;
            -- a job invisible for want of a clearance verdict never heals
            -- unless the sweep looks for holes in every check, not just the
            -- first one.
            OR NOT EXISTS (
                SELECT 1 FROM ai_queries c WHERE c.url = j.url
                  AND c.check_type = 'clearance' AND c.status IN ('passed', 'rejected'))
        )
        LIMIT 4000
        """
    )
    if not rows:
        _set_progress(task_id, 0, 0, "nothing to verify")
        return
    schema = to_strict_json_schema(VerifyLean)
    specs = [
        BatchSpec(r["url"], _VERIFY_INSTRUCTIONS, r["input_content"][:20000], "VerifyLean", schema)
        for r in rows
    ]
    by_url = {r["url"]: r for r in rows}
    _set_progress(task_id, 0, len(specs), "verify batch submitted (half price)")
    model = ai.DEFAULT_MODELS["openai"]
    hook = _batch_event_hook(task_id, "verify", model)
    existing = _pending_batch_ids(task_id)
    if existing:
        from core.batch import collect_batches

        results = await collect_batches(existing, hook)
    else:
        results = await run_responses_batch(specs, model, "low", 1000, on_event=hook)
    done = 0
    for url, res in results.items():
        job = by_url.get(url)
        if job is None or res.error or not res.text:
            continue
        try:
            parsed = VerifyLean.model_validate_json(res.text)
        except Exception:
            continue
        usage = {
            "prompt_tokens": (res.usage or {}).get("input_tokens", 0),
            "completion_tokens": (res.usage or {}).get("output_tokens", 0),
            "total_tokens": (res.usage or {}).get("total_tokens", 0),
        }
        # Write ONLY the verdicts this job was actually missing. A job is
        # selected when EITHER check has a hole, but the text we just read is
        # whatever was last cached — which can predate a closure that another
        # path already recorded. Writing both would let a stale page overturn
        # a fresh 'closed' rejection, and latest-row-wins would put the dead
        # posting back on people's boards.
        if job["needs_closed"]:
            verdicts.record_ai_verdict(
                url=url, check_type="closed",
                rejected=parsed.is_closed, reason="", parsed_json=res.text,
                model=model, company=job["company"], job_title=job["title"],
                context="verify-batch", usage=usage, batched=True,
                batch_id=res.batch_id,
            )
        if job["needs_clearance"]:
            verdicts.record_ai_verdict(
                url=url, check_type="clearance",
                rejected=parsed.requires_clearance_or_restrictions, reason="",
                parsed_json=res.text, model=model,
                company=job["company"], job_title=job["title"],
                context="verify-batch",
                usage=usage if not job["needs_closed"] else {},
                batched=True, batch_id=res.batch_id,
            )
        done += 1
        if done % 200 == 0:
            _set_progress(task_id, done, len(specs), "verified")
    _set_progress(task_id, done, len(specs), "verified")


HANDLERS = {
    "extract_upload": lambda task_id, payload: handle_extract_upload(payload),
    "run_filter": handle_run_filter,
    "run_all_filters": handle_run_all_filters,
    "run_filter_chunk": handle_run_filter_chunk,
    "run_filter_batch_chunk": handle_run_filter_batch_chunk,
    "ingest_source": handle_ingest_source,
    "reverify_open": handle_reverify_open,
    "reverify_chunk": handle_reverify_chunk,
    "send_digests": handle_send_digests,
    "extract_comp": handle_extract_comp,
    "verify_new": handle_verify_new,
    "fetch_missing_content": handle_fetch_missing_content,
    "data_health": handle_data_health,
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
    # Hourly, but only when the previous pass has finished. Each run is capped
    # at EXTRACT_COMP_PER_CYCLE jobs and then waits on the Batch API, which can
    # take hours - enqueuing unconditionally every hour would stack passes up
    # until all three workers were doing nothing else. The dedupe key stops two
    # tasks per cycle; this stops overlap ACROSS cycles.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'extract_comp' "
        "AND status IN ('pending', 'running', 'waiting') LIMIT 1"
    ):
        enqueue("extract_comp", {"cycle": cycle}, dedupe_key=f"comp:{cycle}")
    enqueue("send_digests", {"cycle": day}, dedupe_key=f"digest:{day}")
    enqueue("data_health", {"cycle": cycle}, dedupe_key=f"health:{cycle}")
    # Hourly sweep for jobs the ingest pipeline left unverified (inline AI
    # checks disabled fleet-side): closed+clearance in one batched call each.
    enqueue("verify_new", {"cycle": cycle}, dedupe_key=f"verify:{cycle}")
    # Backlog walker: jobs ingested before content-caching existed (or whose
    # scrape failed) can never be checked until their page is cached.
    enqueue(
        "fetch_missing_content", {"cycle": cycle}, dedupe_key=f"content:{cycle}"
    )


_current_task_id: Optional[int] = None


def _graceful_exit(signum: int, frame: Any) -> None:
    """Deploys must not leave the in-flight task in 'running' limbo until the
    reaper times out: requeue it immediately (chunks resume from cached
    verdicts) without burning an attempt, then exit."""
    if _current_task_id is not None:
        try:
            db.execute(
                "UPDATE tasks SET status = 'pending', attempts = GREATEST(attempts - 1, 0), "
                "started_at = NULL, last_heartbeat = NULL "
                "WHERE id = %s AND status = 'running'",
                (_current_task_id,),
            )
            logger.info(f"SIGTERM: requeued task {_current_task_id}, exiting")
        except Exception:
            pass
    os._exit(0)


# Host resource exhaustion (small fleet hosts hitting their memory ceiling
# while chromium is up). The task is fine; the machine momentarily isn't.
_TRANSIENT_MARKERS = (
    "can't start new thread",
    "cannot allocate memory",
    "resource temporarily unavailable",
    "out of memory",
    "no space left on device",
)


def _is_transient(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)


def _report_worker_status(current_task_id: Optional[int]) -> None:
    try:
        db.execute(
            """
            INSERT INTO worker_status (name, current_task_id, last_seen)
            VALUES (%(name)s, %(tid)s, now())
            ON CONFLICT (name) DO UPDATE SET
                current_task_id = %(tid)s, last_seen = now()
            """,
            {"name": WORKER_NAME, "tid": current_task_id},
        )
    except Exception:
        logger.exception("worker status report failed")


async def run_once() -> bool:
    global _current_task_id
    task = _claim_task()
    if not task:
        _report_worker_status(None)
        return False
    _current_task_id = task["id"]
    _report_worker_status(task["id"])
    handler = HANDLERS.get(task["kind"])
    events.publish_task(task["id"])
    logger.info(f"Task {task['id']} ({task['kind']}) starting")
    if not handler:
        _finish(task["id"], "failed", f"unknown task kind: {task['kind']}")
        return True
    task_start = time.monotonic()

    async def _liveness() -> None:
        # Progress-based heartbeats stall when every job in flight is slow;
        # this proves the process is alive so the reaper only requeues tasks
        # whose worker actually died. Also keeps worker_status fresh so a
        # host deep in a long chunk never reads as dead.
        while True:
            await asyncio.sleep(60)
            db.execute(
                "UPDATE tasks SET last_heartbeat = now() "
                "WHERE id = %s AND status = 'running'",
                (task["id"],),
            )
            _report_worker_status(task["id"])

    hb = asyncio.create_task(_liveness())
    try:
        await handler(task["id"], task["payload"])
        _finish(task["id"], "done")
        metrics.TASKS_PROCESSED.labels(task["kind"], "done").inc()
        logger.info(f"Task {task['id']} done")
    except Exception as exc:
        if _is_transient(exc) and task["attempts"] < MAX_ATTEMPTS:
            # Host ran out of memory/threads, not a broken task: put it back so
            # a healthier worker (or this one, later) takes it. Failing
            # permanently here costs the source a whole ingest cycle.
            db.execute(
                "UPDATE tasks SET status = 'pending', started_at = NULL, "
                "last_heartbeat = NULL, error = %s WHERE id = %s AND status = 'running'",
                (f"retrying after transient error: {str(exc)[:200]}", task["id"]),
            )
            events.publish_task(task["id"])
            metrics.TASKS_PROCESSED.labels(task["kind"], "requeued").inc()
            logger.warning(f"Task {task['id']} hit a transient error, requeued: {exc}")
        else:
            _finish(task["id"], "failed", str(exc))
            metrics.TASKS_PROCESSED.labels(task["kind"], "failed").inc()
            logger.exception(f"Task {task['id']} failed")
    finally:
        hb.cancel()
        _current_task_id = None
    metrics.TASK_DURATION.labels(task["kind"]).observe(time.monotonic() - task_start)
    if task["kind"] in CHUNK_KINDS:
        try:
            _maybe_finalize_parent(task["payload"]["parent_id"])
        except Exception:
            logger.exception("parent finalize failed")
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _graceful_exit)
    signal.signal(signal.SIGINT, _graceful_exit)
    db.init_schema()
    db.execute(
        "INSERT INTO worker_status (name) VALUES (%s) ON CONFLICT (name) DO UPDATE "
        "SET started_at = now(), current_task_id = NULL, last_seen = now()",
        (WORKER_NAME,),
    )
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
