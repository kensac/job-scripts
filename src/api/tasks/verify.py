"""Closed/clearance verification and the daily re-verification sweep."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from api import ai, db, events, verdicts
from api.batch_results import progress_counts
from api.tasks.board import UNTOUCHED, demote_closed
from api.tasks.models import _VERIFY_INSTRUCTIONS, VerifyVerdict
from api.tasks.runtime import (
    CHUNK_SIZE,
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
    batch_event_hook,
    cancelled,
    collect_pending,
    consume_result,
    enqueue,
    has_batch_work,
    parent_cancelled,
    run_batched,
    set_progress,
    submit_or_collect,
    update_parent_progress,
)
from core.providers.spec import StructuredOutput
from core.routing import TaskShape, resolve
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL

logger = logging.getLogger("jobtracker_worker")


REVERIFY_DAYS = int(os.environ.get("JOBTRACKER_REVERIFY_DAYS", "7"))


REVERIFY_PER_CYCLE = int(os.environ.get("JOBTRACKER_REVERIFY_PER_CYCLE", "0"))  # 0 = all stale


def _newer_evidence(result, check: str) -> bool:
    """Something decided this check after the batch was submitted, so the
    batch's answer is stale on arrival and must not overwrite it."""
    if not result.batch_id:
        return False
    return bool(
        db.query_one(
            "SELECT 1 FROM ai_queries q JOIN ai_batches b ON b.provider_batch_id = %s "
            "WHERE q.url = %s AND q.check_type = %s "
            "AND q.status IN ('passed', 'rejected') AND q.created_at > b.submitted_at LIMIT 1",
            (result.batch_id, result.custom_id, check),
        )
    )


def _record_reverify_results(task_id: int, results: list) -> int:
    recorded = 0
    for res in results:
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            job = res.request.context if res.request else None
            if not job:
                receipt.outcome = "unknown_request"
                continue
            if res.error or not res.text:
                receipt.outcome = "failed"
                continue
            # Stale on one axis is stale on both: the guard is about the page
            # text being older than somebody else's, not about which question
            # was asked of it. A parked batch that lost the race on closed
            # must not write a clearance verdict from the same old page.
            if any(_newer_evidence(res, check) for check in ("closed", "clearance")):
                receipt.outcome = "superseded"
                continue
            try:
                parsed = VerifyVerdict.model_validate_json(res.text)
            except ValueError:
                logger.warning("reverify: unparsable batch output for %s", res.custom_id)
                receipt.outcome = "invalid_output"
                continue
            # Both axes, from one answer on one fetch. The sweep used to ask
            # only whether the posting had closed, which is why a clearance
            # verdict was written once and never again: nothing else revisits
            # it. The page is already fetched and the call is already made, so
            # the second axis costs its output tokens and nothing else.
            written = False
            # One call, two rows, and the usage books onto the first one
            # written. The second is a zero-token decided row, which is the
            # same spelling handle_verify_new uses below and the shape
            # routers/spend.py reads as a joint call.
            usage = ai.batch_usage(res.usage)
            for check, rejected, reason in (
                ("closed", parsed.is_closed, parsed.closed_reason),
                ("clearance", parsed.requires_clearance_or_restrictions, parsed.clearance_reason),
            ):
                verdicts.record_ai_verdict(
                    url=res.custom_id,
                    check_type=check,
                    rejected=rejected,
                    reason=reason,
                    parsed_json=res.text,
                    model=res.model,
                    usage=usage,
                    company=job["company"],
                    job_title=job["title"],
                    context="reverify",
                    batched=True,
                    batch_id=res.batch_id,
                )
                usage = {}
                written = True
            receipt.outcome = "written" if written else "superseded"
            if not written:
                continue
            recorded += 1
    return recorded


async def _reverify_jobs(
    task_id: int,
    rows: list[dict[str, Any]],
    parent_id: int | None = None,
    force: bool = False,
) -> None:
    """Two phases: gather evidence concurrently (ATS gone-detection, then
    content, the fleet-distributed, network-bound part), then settle every
    remaining verdict in ONE half-price batch instead of a call per job."""
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    if has_batch_work(task_id):
        results = await collect_pending(task_id, batch_event_hook(task_id, "reverify", None))
        _record_reverify_results(task_id, results)
        done, total = progress_counts(task_id)
        set_progress(task_id, done, total, "reverified")
        if parent_id:
            update_parent_progress(parent_id)
        return
    if not ai.server_key("openai"):
        raise LookupError("no server OpenAI key for reverification")
    model = resolve(VERIFY_TASK).model
    by_url = {r["url"]: r for r in rows}
    hook = batch_event_hook(task_id, "reverify", model)

    # Resumability: a requeued chunk skips rows already re-verified this cycle.
    # A forced sweep skips nothing. The point is to overturn existing verdicts.
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
                set_progress(task_id, done, total, "checking open status")
                if parent_id:
                    update_parent_progress(parent_id)
        if cancelled(task_id) or (parent_id and parent_cancelled(parent_id)):
            for t in pending:
                t.cancel()
            return

    if needs_ai:
        set_progress(task_id, done, total, f"batch of {len(needs_ai)} submitted (half price)")
        if parent_id:
            update_parent_progress(parent_id)
        schema = to_strict_json_schema(VerifyVerdict)
        specs = [
            BatchSpec(
                url,
                _VERIFY_INSTRUCTIONS,
                content[:20000],
                "VerifyVerdict",
                schema,
                context=by_url[url],
            )
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
        _record_reverify_results(task_id, results)
    set_progress(task_id, total, total, "reverified")
    if parent_id:
        update_parent_progress(parent_id)


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
    staleness, board membership and the per-cycle cap, for when the evidence
    behind existing verdicts is itself suspect (e.g. verdicts taken before the
    fetcher could tell a redirect from a live page)."""
    if has_batch_work(task_id):
        await _reverify_jobs(task_id, [], force=bool(payload.get("full")))
        demote_closed()
        return
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
            SELECT url, company, title FROM (
                SELECT DISTINCT j.url, j.company, j.title FROM user_jobs uj
                JOIN jobs j ON j.id = uj.job_id
                WHERE {UNTOUCHED}
                  AND COALESCE((SELECT MAX(q.created_at) FROM ai_queries q
                                WHERE q.url = j.url AND q.check_type = 'closed'),
                               '-infinity') < now() - make_interval(days => %(days)s)
                {cap_sql}
            ) stale
            UNION
            -- A posting its feed dropped and has since put back. Nothing else
            -- reaches it: a closed verdict removed its board row through
            -- demote_closed, so the branch above cannot see it, and the full
            -- run asks for verdicts that PASSED, so that cannot either. A
            -- closure was therefore permanent, held on the page copy taken
            -- the moment it closed.
            --
            -- Keyed on the re-listing rather than a timer on purpose: a sweep
            -- over everything ever closed grows without bound and is mostly
            -- postings that can no longer change - on 2026-09-10, 464 of
            -- 1,125 belonged to sources since switched off. This costs one
            -- check per posting a feed actually puts back, and it lands in
            -- reverify rather than verify_new because reverify re-fetches:
            -- verify_new judges from the cached copy, which for a closed
            -- posting is the copy that showed it closed.
            --
            -- Self-clearing: the fresh verdict is newer than the return.
            SELECT j.url, j.company, j.title FROM jobs j
            WHERE j.active AND {AI_ELIGIBLE_JOB.format(job="j")}
              AND (SELECT MAX(e.at) FROM job_listing_events e
                   WHERE e.job_id = j.id AND e.listed) > COALESCE(
                    (SELECT MAX(q.created_at) FROM ai_queries q
                     WHERE q.url = j.url AND q.check_type = 'closed'), '-infinity')
            """,
            {"days": REVERIFY_DAYS, "cap": REVERIFY_PER_CYCLE},
        )
    if not rows:
        set_progress(task_id, 0, 0, "nothing stale")
        demote_closed()
        return
    if len(rows) <= CHUNK_SIZE:
        await _reverify_jobs(task_id, rows, force=bool(payload.get("full")))
        demote_closed()
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
    half-price call per job yields both verdicts. Idempotent by re-sweep.
    Only successful lines produce verdict rows; anything missed or failed is
    picked up by the next cycle's sweep."""
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    specs = []
    if not has_batch_work(task_id):
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
            set_progress(task_id, 0, 0, "nothing to verify")
            return
        schema = to_strict_json_schema(VerifyVerdict)
        specs = [
            BatchSpec(
                r["url"],
                _VERIFY_INSTRUCTIONS,
                r["input_content"][:20000],
                "VerifyVerdict",
                schema,
                context={
                    key: r[key] for key in ("company", "title", "needs_closed", "needs_clearance")
                },
            )
            for r in rows
        ]
        set_progress(task_id, 0, len(specs), "verify batch submitted")
    results, _ = await run_batched(task_id, VERIFY_TASK, specs)
    for res in results:
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            job = res.request.context if res.request else None
            if not job:
                receipt.outcome = "unknown_request"
                continue
            if res.error or not res.text:
                receipt.outcome = "failed"
                continue
            try:
                parsed = VerifyVerdict.model_validate_json(res.text)
            except ValueError:
                logger.warning("verify_new: unparsable batch output for %s", res.custom_id)
                receipt.outcome = "invalid_output"
                continue
            # A verdict settled after submission takes precedence over this
            # missing-check request, even if its original snapshot asked for it.
            settled = {
                row["check_type"]
                for row in db.query(
                    "SELECT DISTINCT check_type FROM ai_queries WHERE url = %s "
                    "AND check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')",
                    (res.custom_id,),
                )
            }
            usage = ai.batch_usage(res.usage)
            written = False
            for check, rejected, reason in (
                ("closed", parsed.is_closed, parsed.closed_reason),
                ("clearance", parsed.requires_clearance_or_restrictions, parsed.clearance_reason),
            ):
                if job.get(f"needs_{check}") and check not in settled:
                    verdicts.record_ai_verdict(
                        url=res.custom_id,
                        check_type=check,
                        rejected=rejected,
                        reason=reason,
                        parsed_json=res.text,
                        model=res.model,
                        company=job["company"],
                        job_title=job["title"],
                        context="verify-batch",
                        usage=usage,
                        batched=True,
                        batch_id=res.batch_id,
                    )
                    usage = {}
                    written = True
            receipt.outcome = "written" if written else "superseded"
    done, total = progress_counts(task_id)
    set_progress(task_id, done, total, "verified")
