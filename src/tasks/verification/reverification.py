"""Refresh standing closed and clearance verdicts from current pages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import ai, db, metrics
from api.ai import verdicts
from api.ai.batch_results import progress_counts
from api.board.person_state import UNTOUCHED
from core import catalog, routing, verdict_reads
from core.answers import VERIFICATION_REQUEST
from core.store import AI_ELIGIBLE_JOB, Page, add_ai_results, ai_result_row
from tasks.board import demote_closed
from tasks.runtime import (
    SCRAPE_CONCURRENCY,
    AdaptiveLimiter,
    batch_event_hook,
    cancelled,
    collect_pending,
    consume_result,
    enqueue,
    has_batch_work,
    parent_cancelled,
    park_waiting,
    set_progress,
    submit_or_collect,
    update_parent_progress,
)
from tasks.verification.questions import (
    _submission_model,
    _verification_spec,
    question_sha256,
)

logger = logging.getLogger(__name__)


def _reuse_unchanged(model: str, fetched: list[tuple[str, str]], jobs: dict[str, dict]) -> set[str]:
    """Record the standing answers for pages that have not changed; return their urls.

    Measured on production for 2026-09-27 to 10-04: 5,780 of 7,244 re-checks
    sent the same model byte-identical page text, and 5,776 of them came back
    with the answer already held. The sweep exists to notice a page that
    changed, and these had not, so the call bought nothing but noise.

    Reuse requires the latest closed AND clearance verdicts to have answered
    this exact question. A verdict without a recorded question (legacy, manual,
    ATS-gone) never matches, so it is asked again, and that answer carries the
    hash forward. The rows written are model-less like record_manual's, because
    no call was made, so spend and call counts stay true; they reset the
    staleness clock exactly as a paid verdict would.
    """
    questions = {
        url: question_sha256(model, _verification_spec(url, text)) for url, text in fetched
    }
    if not questions:
        return set()
    latest = verdict_reads.latest_checks(list(questions))
    rows = [
        ai_result_row(
            url,
            verdict.status,
            verdict.reason,
            check,
            company=jobs[url]["company"],
            job_title=jobs[url]["title"],
            config_name="reverify-unchanged",
            request_sha256=questions[url],
        )
        for url, checks in latest.items()
        if len(checks) == 2
        and all(verdict.request_sha256 == questions[url] for verdict in checks.values())
        for check, verdict in checks.items()
    ]
    add_ai_results(rows)
    for row in rows:
        metrics.CHECKS.labels(row["check_type"], row["status"]).inc()
    return {row["url"] for row in rows}


def _newer_evidence(result, check: str) -> bool:
    """Something decided this check after the batch was submitted, so the
    batch's answer is stale on arrival and must not overwrite it."""
    if not result.batch_id:
        return False
    return bool(
        db.query_one(
            "SELECT 1 FROM verdicts q JOIN ai_batches b ON b.provider_batch_id = %s "
            "WHERE q.url = %s AND q.check_type = %s "
            "AND q.created_at > b.submitted_at LIMIT 1",
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
                parsed = VERIFICATION_REQUEST.response_model.model_validate_json(res.text)
            except ValueError:
                logger.warning("reverify: unparsable batch output for %s", res.custom_id)
                receipt.outcome = "invalid_output"
                continue
            # Both axes, from one answer on one fetch. The sweep used to ask
            # only whether the posting had closed, which is why a clearance
            # verdict was written once and never again: nothing else revisits
            # it. The page is already fetched and the call is already made, so
            # the second axis costs its output tokens and nothing else.
            # One call, two rows, and the usage books onto the first one
            # written. The second is a zero-token decided row, which is the
            # same spelling handle_verify_new uses below and the shape
            # routers/spend.py reads as a joint call.
            usage = ai.batch_usage(res.usage)
            shared_call = False
            for check, rejected, reason in (
                ("closed", parsed.is_closed, parsed.closed_reason),
                ("clearance", parsed.requires_clearance_or_restrictions, parsed.clearance_reason),
            ):
                verdicts.record_ai_verdict(
                    verdicts.Verdict(
                        url=res.custom_id,
                        check_type=check,
                        rejected=rejected,
                        reason=reason,
                        parsed_json=res.text,
                        model=res.model,
                        usage=usage,
                        shared_call=shared_call,
                        company=job["company"],
                        job_title=job["title"],
                        page_fetch_id=job.get("page_fetch_id"),
                        context="reverify",
                        batched=True,
                        batch_id=res.batch_id,
                        request_sha256=question_sha256(res.model, res.request),
                    )
                )
                usage = {}
                shared_call = True
            # Both axes are answered unconditionally here, unlike verify_new,
            # which writes only the checks its request was missing. The
            # staleness guard above is what decides whether this result writes
            # at all, so reaching here is the written outcome.
            receipt.outcome = "written"
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
    if has_batch_work(task_id):
        results = await collect_pending(task_id, batch_event_hook(task_id, "reverify", None))
        _record_reverify_results(task_id, results)
        done, total = progress_counts(task_id)
        set_progress(task_id, done, total, "reverified")
        if parent_id:
            update_parent_progress(parent_id)
        return
    if not routing.server_key("openai"):
        raise LookupError("no server OpenAI key for reverification")
    # The model and effort verify_new submits with, configured override included.
    model, effort = _submission_model()
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
        # A page that keeps coming back empty waits longer each time and is
        # eventually given up on; a forced sweep is an admin's call and asks
        # anyway, like a manual re-check.
        parked = verdicts.fetch_parked_urls([r["url"] for r in rows])
        rows = [r for r in rows if r["url"] not in fresh and r["url"] not in parked]
        total = len(rows) + len(fresh)
        done = len(fresh)
    limiter = AdaptiveLimiter()
    scrape_sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)
    needs_ai: list[tuple[str, Page]] = []

    async def gather(r: dict[str, Any]) -> None:
        page, _closure = await verdicts.refresh_page(
            r["url"],
            company=r["company"],
            job_title=r["title"],
            context="reverify",
            scrape_sem=scrape_sem,
        )
        if not page:
            # Either refresh_page already recorded the closure (ATS gone,
            # or the link bounced to a board index), or the fetch simply
            # failed - which says nothing about the job, so the prior verdict
            # stands and the next cycle retries.
            return
        needs_ai.append((r["url"], page))

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

    # A forced sweep is an admin doubting the answers themselves, so it pays.
    if not force:
        reused = _reuse_unchanged(model, [(url, page.text) for url, page in needs_ai], by_url)
        needs_ai = [(url, page) for url, page in needs_ai if url not in reused]
    if needs_ai:
        set_progress(task_id, done, total, f"batch of {len(needs_ai)} submitted (half price)")
        if parent_id:
            update_parent_progress(parent_id)
        specs = [
            _verification_spec(url, page.text, {**by_url[url], "page_fetch_id": page.fetch_id})
            for url, page in needs_ai
        ]
        results = await submit_or_collect(
            task_id,
            specs,
            model,
            effort,
            VERIFICATION_REQUEST.max_output_tokens,
            hook,
        )
        _record_reverify_results(task_id, results)
    set_progress(task_id, total, total, "reverified")
    if parent_id:
        update_parent_progress(parent_id)


async def handle_reverify_open(task_id: int, payload: dict[str, Any]) -> None:
    """Splitter: find every stale working-set pair, shard the re-checks
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
            WHERE {catalog.IS_AVAILABLE.format(job="j")} AND {AI_ELIGIBLE_JOB.format(job="j")} AND {verdict_reads.latest_status("j.url", "closed")} = 'passed'
            ORDER BY j.id
            """
        )
    else:
        cap = int(db.get_config("reverify_per_cycle"))
        cap_sql = "LIMIT %(cap)s" if cap else ""
        rows = db.query(
            f"""
            SELECT url, company, title FROM (
                SELECT DISTINCT j.url, j.company, j.title FROM user_job_working_set ws
                JOIN jobs j ON j.id = ws.job_id
                -- A pair the person acted on is theirs, not the sweep's.
                WHERE NOT EXISTS (
                    SELECT 1 FROM user_jobs uj
                    WHERE uj.user_id = ws.user_id AND uj.job_id = ws.job_id
                      AND NOT ({UNTOUCHED}))
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
            -- Self-clearing: the fresh verdict is newer than the return. A
            -- return is a source's reappeared observation; job_listing_events
            -- held them before, and is frozen.
            --
            -- Only a posting that already has a closed verdict. One never
            -- checked is verify_new's, whose gate skips what no board or
            -- filter can keep; read here it skipped that gate. On production,
            -- 2026-10-11 to 10-18, 1,407 of 3,209 paid re-checks were
            -- such first reads, most from feeds that keep re-listing.
            SELECT j.url, j.company, j.title FROM jobs j
            WHERE {catalog.IS_AVAILABLE.format(job="j")} AND {AI_ELIGIBLE_JOB.format(job="j")}
              AND (SELECT MAX(o.at) FROM source_observations o
                   WHERE o.job_id = j.id AND o.kind = 'reappeared') >
                  (SELECT MAX(q.created_at) FROM ai_queries q
                   WHERE q.url = j.url AND q.check_type = 'closed')
            """,
            {"days": int(db.get_config("reverify_days")), "cap": cap},
        )
    if not rows:
        set_progress(task_id, 0, 0, "nothing stale")
        demote_closed()
        return
    chunk_size = int(db.get_config("filter_chunk_size"))
    if len(rows) <= chunk_size:
        await _reverify_jobs(task_id, rows, force=bool(payload.get("full")))
        demote_closed()
        return
    total = len(rows)
    n_chunks = 0
    for start in range(0, total, chunk_size):
        enqueue(
            "reverify_chunk",
            {
                "parent_id": task_id,
                "rows": rows[start : start + chunk_size],
                "force": bool(payload.get("full")),
            },
        )
        n_chunks += 1
    park_waiting(task_id, total, f"{n_chunks} chunks across the fleet")


async def handle_reverify_chunk(task_id: int, payload: dict[str, Any]) -> None:
    await _reverify_jobs(
        task_id,
        payload["rows"],
        parent_id=payload["parent_id"],
        force=bool(payload.get("force")),
    )
