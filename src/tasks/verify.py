"""Closed/clearance verification and the daily re-verification sweep."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from api import ai, db, managed_board_runs, metrics
from api.ai import verdicts
from api.ai.batch_results import progress_counts
from api.task_config import configured_model, configured_shape
from core import near_copy, routing, verdict_reads
from core.answers import (
    VERIFICATION_REQUEST,
    FilterDecision,
    joint_question_key,
    joint_verification,
)
from core.batch import BatchSpec, structured_response_spec
from core.filters import build_custom_input
from core.routing import resolve
from core.shapes import VERIFY_TASK
from core.store import (
    AI_ELIGIBLE_JOB,
    CONTENT_LATERAL,
    add_ai_results,
    ai_result_row,
    decided_custom_urls,
)
from tasks.board import UNTOUCHED, demote_closed
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
    run_batched,
    set_progress,
    submit_or_collect,
    update_parent_progress,
)

logger = logging.getLogger(__name__)

# Both sweeps submit at the shape's effort; reverify spells the fallback.
_EFFORT = VERIFY_TASK.effort or "low"


def _verification_spec(url: str, content: str, context: dict | None = None) -> BatchSpec:
    return structured_response_spec(
        url,
        VERIFICATION_REQUEST.instructions,
        VERIFICATION_REQUEST.build_input(content),
        VERIFICATION_REQUEST.response_model,
        context=context,
    )


def _submission_model() -> tuple[str, str]:
    """The model and effort run_batched will submit the new-posting sweep with."""
    shape = configured_shape(VERIFY_TASK)
    chosen = resolve(shape, override=configured_model(VERIFY_TASK.purpose))
    return chosen.model, str(chosen.params.get("reasoning_effort") or shape.resolved_effort() or "")


def _joint_spec(
    row: dict[str, Any],
    questions: list[managed_board_runs.BoardQuestion],
    model: str,
    effort: str,
) -> BatchSpec:
    """Verification plus the boards' questions, reading the posting once."""
    instructions, response_model = joint_verification(
        {question.board_id: question.criteria for question in questions}
    )
    content = VERIFICATION_REQUEST.build_input(row["input_content"])
    return structured_response_spec(
        row["url"],
        instructions,
        build_custom_input(row["company"], row["title"], content),
        response_model,
        context={
            **{key: row[key] for key in ("company", "title", "needs_closed", "needs_clearance")},
            "model": model,
            "effort": effort,
            "verify_question": question_sha256(
                model, _verification_spec(row["url"], row["input_content"])
            ),
            "boards": [
                {"board_id": q.board_id, "prompt_hash": q.prompt_hash, "model": q.model}
                for q in questions
            ],
        },
    )


def question_sha256(model: str | None, request: BatchSpec | None) -> str | None:
    """Identity of the question a verification answers, or None if unknown.

    Everything that could change the answer is in it: the model, the effort,
    the schema, the instructions and the exact page text. Hashed from the
    request as submitted (a collected result carries its frozen snapshot), so
    a prompt edit or a model change is a different question and is paid for.
    """
    if not model or request is None:
        return None
    digest = hashlib.sha256()
    for part in (
        model,
        _EFFORT,
        request.schema_name,
        json.dumps(request.schema, sort_keys=True),
        request.instructions,
        request.input,
    ):
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


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
        # A page that keeps coming back empty waits longer each time and is
        # eventually given up on; a forced sweep is an admin's call and asks
        # anyway, like a manual re-check.
        parked = verdicts.fetch_parked_urls([r["url"] for r in rows])
        rows = [r for r in rows if r["url"] not in fresh and r["url"] not in parked]
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

    # A forced sweep is an admin doubting the answers themselves, so it pays.
    if not force:
        reused = _reuse_unchanged(model, needs_ai, by_url)
        needs_ai = [(url, content) for url, content in needs_ai if url not in reused]
    if needs_ai:
        set_progress(task_id, done, total, f"batch of {len(needs_ai)} submitted (half price)")
        if parent_id:
            update_parent_progress(parent_id)
        specs = [_verification_spec(url, content, by_url[url]) for url, content in needs_ai]
        results = await submit_or_collect(
            task_id,
            specs,
            model,
            _EFFORT,
            VERIFICATION_REQUEST.max_output_tokens,
            hook,
        )
        _record_reverify_results(task_id, results)
    set_progress(task_id, total, total, "reverified")
    if parent_id:
        update_parent_progress(parent_id)


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
            WHERE j.active AND {AI_ELIGIBLE_JOB.format(job="j")} AND {verdict_reads.latest_status("j.url", "closed")} = 'passed'
            ORDER BY j.id
            """
        )
    else:
        cap = int(db.get_config("reverify_per_cycle"))
        cap_sql = "LIMIT %(cap)s" if cap else ""
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


def _in_flight(task_id: int) -> list[str]:
    """Postings another parked sweep has already submitted.

    A sweep no longer waits for the previous one to finish (a straggling batch
    held one for 15 hours on 2026-10-08, and no new posting was verified until
    it returned), so each excludes what the others are already paying for.
    """
    return [
        row["custom_id"]
        for row in db.query(
            "SELECT r.custom_id FROM batch_requests r JOIN tasks t ON t.id = r.task_id "
            "WHERE t.kind = 'verify_new' AND t.id <> %s "
            "AND t.status IN ('pending', 'running', 'waiting', 'awaiting_batch')",
            (task_id,),
        )
    ]


def _reuse_near_copies(task_id: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give a posting its verified twin's verdicts; return the ones still to ask.

    A twin is the same source, title and text once places and numbers are
    removed (core.near_copy). A candidate whose twin already holds closed and
    clearance verdicts takes those and the twin's verdicts under every live
    board and filter prompt, as rows marked verify-near-copy that cost nothing.
    One whose twin is in this sweep or in a parked one waits for it, so the
    text is read once. The posting's own location, date and gates still apply
    where each board and filter reads the verdict.
    """
    keys = {r["url"]: near_copy.key(r["title"] or "", r["input_content"] or "") for r in rows}
    db.execute(
        "UPDATE jobs SET near_copy_key = k.key FROM unnest(%s::text[], %s::text[]) AS k(url, key) "
        "WHERE jobs.url = k.url AND jobs.near_copy_key IS DISTINCT FROM k.key",
        (list(keys), list(keys.values())),
    )
    pairs = {(r["source"], keys[r["url"]]) for r in rows}
    sources, digests = [p[0] for p in pairs], [p[1] for p in pairs]
    twins = {
        (row["source"], row["near_copy_key"]): row["url"]
        for row in db.query(
            "SELECT DISTINCT ON (j.source, j.near_copy_key) j.source, j.near_copy_key, j.url "
            "FROM jobs j WHERE (j.source, j.near_copy_key) IN "
            "(SELECT * FROM unnest(%s::text[], %s::text[])) AND NOT (j.url = ANY(%s::text[])) "
            f"AND {verdict_reads.has_verdict('j.url', 'closed')} "
            f"AND {verdict_reads.has_verdict('j.url', 'clearance')} "
            "ORDER BY j.source, j.near_copy_key, j.id DESC",
            (sources, digests, list(keys)),
        )
    }
    pending = {
        (row["source"], row["near_copy_key"])
        for row in db.query(
            "SELECT source, near_copy_key FROM jobs WHERE url = ANY(%s) "
            "AND near_copy_key IS NOT NULL",
            (_in_flight(task_id),),
        )
    }
    ask, reuse, chosen = [], [], set()
    for r in rows:
        pair = (r["source"], keys[r["url"]])
        if pair in twins:
            reuse.append((r, twins[pair]))
        elif pair in pending or pair in chosen:
            continue
        else:
            chosen.add(pair)
            ask.append(r)
    if reuse:
        _copy_twin_verdicts(reuse)
    return ask


def _copy_twin_verdicts(reuse: list[tuple[dict[str, Any], str]]) -> None:
    twin_urls = sorted({twin for _, twin in reuse})
    checks = verdict_reads.latest_checks(twin_urls)
    customs: dict[str, list[dict[str, Any]]] = {}
    for row in db.query(
        verdict_reads.latest_per(
            "url, prompt_hash, model",
            "url, prompt_hash, model, filter_name, status, reason, parsed_json",
            "url = ANY(%s) AND check_type = 'custom' AND prompt_hash IN ("
            "SELECT prompt_hash FROM managed_boards WHERE published "
            "UNION SELECT prompt_hash FROM user_filters WHERE enabled)",
        ),
        (twin_urls,),
    ):
        customs.setdefault(row["url"], []).append(row)
    decided = {
        (row["url"], row["prompt_hash"], row["model"])
        for row in db.query(
            "SELECT DISTINCT url, prompt_hash, model FROM verdicts WHERE url = ANY(%s) "
            "AND check_type = 'custom'",
            ([job["url"] for job, _ in reuse],),
        )
    }
    rows = []
    for job, twin in reuse:
        for check in ("closed", "clearance"):
            verdict = checks.get(twin, {}).get(check)
            if job[f"needs_{check}"] and verdict:
                rows.append(
                    ai_result_row(
                        job["url"],
                        verdict.status,
                        verdict.reason,
                        check,
                        company=job["company"],
                        job_title=job["title"],
                        config_name="verify-near-copy",
                    )
                )
        for verdict in customs.get(twin, []):
            if (job["url"], verdict["prompt_hash"], verdict["model"]) in decided:
                continue
            rows.append(
                ai_result_row(
                    job["url"],
                    verdict["status"],
                    verdict["reason"],
                    "custom",
                    model=verdict["model"],
                    filter_name=verdict["filter_name"],
                    prompt_hash=verdict["prompt_hash"],
                    parsed_json=verdict["parsed_json"],
                    company=job["company"],
                    job_title=job["title"],
                    config_name="verify-near-copy",
                )
            )
    add_ai_results(rows)
    for row in rows:
        metrics.CHECKS.labels(row["check_type"], row["status"]).inc()


async def handle_verify_new(task_id: int, payload: dict[str, Any]) -> None:
    """Batched replacement for ingest-time closed/clearance checks: one
    half-price call per job yields both verdicts. Idempotent by re-sweep.
    Only successful lines produce verdict rows; anything missed or failed is
    picked up by the next cycle's sweep."""
    specs = []
    if not has_batch_work(task_id):
        from api import verification_candidates

        candidate_params = verification_candidates.params()
        rows = db.query(
            f"""
            WITH {verification_candidates.TARGETS}, candidates AS (
            SELECT j.url, j.source, j.company, j.title, q.input_content,
                   NOT {verdict_reads.has_verdict("j.url", "closed")} AS needs_closed,
                   NOT {verdict_reads.has_verdict("j.url", "clearance")} AS needs_clearance
            FROM jobs j
            {CONTENT_LATERAL.format(url="j.url", columns="input_content")}
            WHERE j.active AND {verification_candidates.REACHABLE}
              AND NOT (j.url = ANY(%(in_flight)s::text[])) AND (
                NOT {verdict_reads.has_verdict("j.url", "closed")}
                -- Short-circuited pipelines (and any upstream verdict that later
                -- flips to passing) leave downstream checks MISSING, not false;
                -- a job invisible for want of a clearance verdict never heals
                -- unless the sweep looks for holes in every check, not just the
                -- first one.
                OR NOT {verdict_reads.has_verdict("j.url", "clearance")}
            )
            -- Freshest first, as the content sweep already selects. The cap
            -- makes this a priority queue, and `j.id` is ingest order, so a
            -- backlog starves the day's postings: on 2026-09-15, 214,306
            -- active postings held no closed verdict and only 3,440 of them
            -- were posted within three days. A posting that arrived that
            -- morning waited out fifty cycles behind postings months old.
            -- Board candidacy requires this verdict, so the managed boards
            -- showed nothing new while the sweep worked. Ordered, one cycle
            -- covers every genuinely fresh posting and the stale remainder
            -- drains behind it.
            ORDER BY j.date_posted DESC NULLS LAST
            LIMIT %(cap)s
            )
            SELECT * FROM candidates
            """,
            {
                **candidate_params,
                "cap": int(db.get_config("verify_new_per_cycle")),
                "in_flight": _in_flight(task_id),
            },
        )
        if not rows:
            set_progress(task_id, 0, 0, "nothing to verify")
            return
        if db.get_config("verify_near_copy_reuse"):
            rows = _reuse_near_copies(task_id, rows)
            if not rows:
                set_progress(task_id, 0, 0, "every candidate reused a twin's verdicts")
                return
        model, effort = _submission_model()
        questions = (
            managed_board_runs.verification_questions(rows, model, effort)
            if db.get_config("verify_answers_board_questions")
            else {}
        )
        specs = [
            _joint_spec(r, questions[r["url"]], model, effort)
            if questions.get(r["url"])
            else _verification_spec(
                r["url"],
                r["input_content"],
                {key: r[key] for key in ("company", "title", "needs_closed", "needs_clearance")},
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
            boards = job.get("boards") or []
            decisions: dict[int, FilterDecision] = {}
            try:
                if boards:
                    answer = json.loads(res.text)
                    parsed = VERIFICATION_REQUEST.response_model.model_validate(
                        answer["verification"]
                    )
                    decisions = {
                        board["board_id"]: FilterDecision.model_validate(
                            answer[joint_question_key(board["board_id"])]
                        )
                        for board in boards
                    }
                else:
                    parsed = VERIFICATION_REQUEST.response_model.model_validate_json(res.text)
            except (ValueError, KeyError, TypeError):
                logger.warning("verify_new: unparsable batch output for %s", res.custom_id)
                receipt.outcome = "invalid_output"
                continue
            if boards:
                parsed_json = parsed.model_dump_json()
                # The question verification answered, as if it had been asked alone,
                # so reverify's unchanged-page reuse still recognises it.
                question = job.get("verify_question") if res.model == job.get("model") else None
            else:
                parsed_json = res.text
                question = question_sha256(res.model, res.request)
            # A verdict settled after submission takes precedence over this
            # missing-check request, even if its original snapshot asked for it.
            settled = {
                row["check_type"]
                for row in db.query(
                    "SELECT DISTINCT check_type FROM verdicts WHERE url = %s "
                    "AND check_type IN ('closed', 'clearance')",
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
                        verdicts.Verdict(
                            url=res.custom_id,
                            check_type=check,
                            rejected=rejected,
                            reason=reason,
                            parsed_json=parsed_json,
                            model=res.model,
                            company=job["company"],
                            job_title=job["title"],
                            context="verify-batch",
                            usage=usage,
                            shared_call=written,
                            batched=True,
                            batch_id=res.batch_id,
                            request_sha256=question,
                        )
                    )
                    usage = {}
                    written = True
            for board in boards:
                # A verdict is keyed by the model that gave it; one the board did not
                # ask for, or that its own run has bought meanwhile, is not written.
                if res.model != board["model"] or decided_custom_urls(
                    [res.custom_id], board["prompt_hash"], model=res.model
                ):
                    continue
                verdicts.record_ai_verdict(
                    verdicts.Verdict(
                        url=res.custom_id,
                        check_type="custom",
                        rejected=decisions[board["board_id"]].should_filter,
                        reason=None,
                        parsed_json=decisions[board["board_id"]].model_dump_json(),
                        model=res.model,
                        company=job["company"],
                        job_title=job["title"],
                        instructions=res.request.instructions if res.request else None,
                        input_text=res.request.input if res.request else None,
                        filter_name=f"managed-board:{board['board_id']}",
                        prompt_hash=board["prompt_hash"],
                        context="verify-batch",
                        usage=usage,
                        shared_call=written,
                        batched=True,
                        batch_id=res.batch_id,
                        reasoning_effort=job.get("effort"),
                    )
                )
                usage = {}
                written = True
            receipt.outcome = "written" if written else "superseded"
    done, total = progress_counts(task_id)
    set_progress(task_id, done, total, "verified")
