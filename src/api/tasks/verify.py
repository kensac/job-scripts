"""Closed/clearance verification and the daily re-verification sweep."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from api import ai, db, events, verdicts
from api.tasks.board import _UNTOUCHED, _demote_closed
from api.tasks.models import _VERIFY_INSTRUCTIONS, JobClosedVerdict, VerifyVerdict
from api.tasks.runtime import (
    CHUNK_SIZE,
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
    _batch_event_hook,
    _cancelled,
    _parent_cancelled,
    _pending_batch_ids,
    _set_progress,
    _update_parent_progress,
    enqueue,
    submit_or_collect,
)
from core.providers.spec import StructuredOutput
from core.routing import TaskShape, resolve
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL

logger = logging.getLogger("jobtracker_worker")


REVERIFY_DAYS = int(os.environ.get("JOBTRACKER_REVERIFY_DAYS", "7"))


REVERIFY_PER_CYCLE = int(os.environ.get("JOBTRACKER_REVERIFY_PER_CYCLE", "0"))  # 0 = all stale


def _evidence_superseded(results: dict[str, Any]) -> set[str]:
    """Urls whose closed verdict was settled AFTER the evidence in hand.

    A batched reverify scrapes a page, submits, and parks; poll_batches
    resumes it whenever the provider finishes, which can be hours later. The
    page text in that batch is therefore as old as the submission, and in the
    meantime another path (ATS gone-detection, a fresh sweep) may have recorded
    a closure from newer evidence. Verdicts are append-only and the latest row
    wins, so writing ours last would overturn that with a stale page and put a
    dead posting back on people's boards.

    The batch's own submitted_at is the age of the evidence: everything in the
    request was gathered before it. A url with no batch id or no registry row
    cannot be dated, so it records as before rather than being dropped on a
    suspicion.
    """
    urls = [u for u, res in results.items() if res.text and not res.error]
    if not urls:
        return set()
    batch_ids = list({res.batch_id for res in results.values() if res.batch_id})
    submitted = {
        row["provider_batch_id"]: row["submitted_at"]
        for row in db.query(
            "SELECT provider_batch_id, submitted_at FROM ai_batches "
            "WHERE provider_batch_id = ANY(%s)",
            (batch_ids,),
        )
    }
    settled = {
        row["url"]: row["created_at"]
        for row in db.query(
            "SELECT DISTINCT ON (url) url, created_at FROM ai_queries "
            "WHERE url = ANY(%s) AND check_type = 'closed' "
            "AND status IN ('passed', 'rejected') ORDER BY url, id DESC",
            (urls,),
        )
    }
    superseded = set()
    for url in urls:
        evidence_at = submitted.get(results[url].batch_id or "")
        latest = settled.get(url)
        if evidence_at is not None and latest is not None and latest > evidence_at:
            superseded.add(url)
    return superseded


def _record_reverify_results(
    results: dict[str, Any], by_url: dict[str, dict[str, Any]], model: str
) -> int:
    """Turns batch lines into closed verdicts. Only parseable lines record —
    anything failed or missing simply stays stale and the next daily sweep
    picks it up, which is what makes the whole task idempotent.

    Recording is guarded at the point of the write rather than in the caller,
    because a resumed chunk reattaches to its batch and returns before the
    caller's staleness filter ever runs. Guarding here covers every path into
    this function by construction.
    """
    superseded = _evidence_superseded(results)
    if superseded:
        logger.info(
            f"reverify: {len(superseded)} url(s) settled by newer evidence "
            "while the batch was in flight; leaving those verdicts alone"
        )
    recorded = 0
    for url, res in results.items():
        job = by_url.get(url)
        if job is None or res.error or not res.text or url in superseded:
            continue
        try:
            parsed = JobClosedVerdict.model_validate_json(res.text)
        except Exception:
            logger.warning(f"reverify: unparsable batch output for {url}")
            continue
        verdicts.record_ai_verdict(
            url=url,
            check_type="closed",
            rejected=parsed.is_closed,
            reason=parsed.reason,
            parsed_json=res.text,
            model=model,
            usage={
                "prompt_tokens": (res.usage or {}).get("input_tokens", 0),
                "completion_tokens": (res.usage or {}).get("output_tokens", 0),
                "total_tokens": (res.usage or {}).get("total_tokens", 0),
            },
            company=job["company"],
            job_title=job["title"],
            context="reverify",
            batched=True,
            batch_id=res.batch_id,
        )
        recorded += 1
    return recorded


async def _reverify_jobs(
    task_id: int,
    rows: list[dict[str, Any]],
    parent_id: int | None = None,
    force: bool = False,
) -> None:
    """Two phases: gather evidence concurrently (ATS gone-detection, then
    content — the fleet-distributed, network-bound part), then settle every
    remaining verdict in ONE half-price batch instead of a call per job."""
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec, collect_batches
    from core.pittcsc_simplify import CLOSED_INSTRUCTIONS

    if not ai.server_key("openai"):
        raise LookupError("no server OpenAI key for reverification")
    model = resolve(VERIFY_TASK).model
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
        cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)
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
    needs_ai: list[tuple] = []

    async def gather(r: dict[str, Any]) -> None:
        content, _closure = await verdicts.refresh_content(
            r["url"],
            company=r["company"],
            job_title=r["title"],
            context="reverify",
            scrape_sem=scrape_sem,
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
    pending: dict[asyncio.Task, dict[str, Any]] = {}
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
        schema = to_strict_json_schema(JobClosedVerdict)
        specs = [
            BatchSpec(url, CLOSED_INSTRUCTIONS, content[:20000], "JobClosedVerdict", schema)
            for url, content in needs_ai
        ]
        results = await submit_or_collect(
            task_id,
            specs,
            model,
            VERIFY_TASK.effort or "low",
            VERIFY_TASK.max_output_tokens,
            hook,
        )
        _record_reverify_results(results, by_url, model)
    _set_progress(task_id, total, total, "reverified")
    if parent_id:
        _update_parent_progress(parent_id)


# Closed/clearance verification, batched. One candidate, so this resolves to
# gpt-5-nano exactly as it did when the name was written inline - the change is
# that a missing key or a model that cannot enforce a schema now fails here,
# with a reason, instead of at the provider after a wave has been built.
#
# Deliberately NOT widened to a second model. tasks/filters.py scopes its
# cached-verdict check by model, so a sweep that answered on a different model
# than last cycle would see no cached verdicts and re-run everything at full
# price. See core/routing.py.
VERIFY_TASK = TaskShape(
    purpose="verify",
    label="Closed and clearance verification",
    notes=(
        "A yes/no read of whether a posting is still open and whether it "
        "demands a clearance. Cheap and high volume - every active job, every "
        "cycle - so the fleet default is the right place to start."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=1000,
    est_prompt_tokens=5500,
    effort="low",
    candidates=("gpt-5-nano",),
)


async def handle_reverify_open(task_id: int, payload: dict[str, Any]) -> None:
    """Splitter: find every stale untouched board row, shard the re-checks
    across the fleet, demote closed rows when the last chunk lands.

    full=true re-checks EVERY active job currently believed open, ignoring
    staleness, board membership and the per-cycle cap — for when the evidence
    behind existing verdicts is itself suspect (e.g. verdicts taken before the
    fetcher could tell a redirect from a live page)."""
    if payload.get("full"):
        # Source-gated like every other sweep. The staleness path below reads
        # user_jobs and is reachable by construction; this one reads `jobs`
        # directly, so a full run was the last way an unreachable posting could
        # still cost money - 504 calls against `internships` in one day.
        rows = db.query(
            f"""
            SELECT j.url, j.company, j.title FROM jobs j
            WHERE j.active AND {AI_ELIGIBLE_JOB.format(job="j")} AND EXISTS (
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


async def handle_reverify_chunk(task_id: int, payload: dict[str, Any]) -> None:
    await _reverify_jobs(
        task_id,
        payload["rows"],
        parent_id=payload["parent_id"],
        force=bool(payload.get("force")),
    )


async def handle_verify_new(task_id: int, payload: dict[str, Any]) -> None:
    """Batched replacement for ingest-time closed/clearance checks: one
    half-price call per job yields both verdicts. Idempotent by re-sweep —
    only successful lines produce verdict rows; anything missed or failed is
    picked up by the next cycle's sweep."""
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    rows = db.query(
        f"""
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
        {CONTENT_LATERAL.format(url="j.url", columns="input_content")}
        WHERE j.active AND {AI_ELIGIBLE_JOB.format(job="j")} AND (
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
    schema = to_strict_json_schema(VerifyVerdict)
    specs = [
        BatchSpec(
            r["url"], _VERIFY_INSTRUCTIONS, r["input_content"][:20000], "VerifyVerdict", schema
        )
        for r in rows
    ]
    by_url = {r["url"]: r for r in rows}
    _set_progress(task_id, 0, len(specs), "verify batch submitted (half price)")
    model = resolve(VERIFY_TASK).model
    hook = _batch_event_hook(task_id, "verify", model)
    existing = _pending_batch_ids(task_id)
    if existing:
        from core.batch import collect_batches

        results = await collect_batches(existing, hook)
    else:
        results = await submit_or_collect(task_id, specs, model, "low", 1000, hook)
    done = 0
    for url, res in results.items():
        job = by_url.get(url)
        if job is None or res.error or not res.text:
            continue
        try:
            parsed = VerifyVerdict.model_validate_json(res.text)
        except Exception:
            logger.warning(f"verify_new: unparsable batch output for {url}")
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
                url=url,
                check_type="closed",
                rejected=parsed.is_closed,
                reason=parsed.closed_reason,
                parsed_json=res.text,
                model=model,
                company=job["company"],
                job_title=job["title"],
                context="verify-batch",
                usage=usage,
                batched=True,
                batch_id=res.batch_id,
            )
        if job["needs_clearance"]:
            verdicts.record_ai_verdict(
                url=url,
                check_type="clearance",
                rejected=parsed.requires_clearance_or_restrictions,
                reason=parsed.clearance_reason,
                parsed_json=res.text,
                model=model,
                company=job["company"],
                job_title=job["title"],
                context="verify-batch",
                usage=usage if not job["needs_closed"] else {},
                batched=True,
                batch_id=res.batch_id,
            )
        done += 1
        if done % 200 == 0:
            _set_progress(task_id, done, len(specs), "verified")
    _set_progress(task_id, done, len(specs), "verified")
