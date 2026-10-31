"""Verify new postings and reuse verified near copies where possible."""

from __future__ import annotations

import json
import logging
from typing import Any

from api import ai, ai_pause, db, managed_boards, metrics
from api.ai import verdicts
from api.ai.batch_results import progress_counts
from core import catalog, near_copy, page_fetches, verdict_reads
from core.answers import VERIFICATION_REQUEST, FilterDecision, joint_question_key
from core.shapes import VERIFY_TASK
from core.store import (
    CONTENT_LATERAL,
    MIN_CONTENT_CHARS,
    add_ai_results,
    ai_result_row,
    decided_custom_urls,
)
from tasks.runtime import consume_result, has_batch_work, run_batched, set_progress
from tasks.verification.questions import (
    _joint_spec,
    _submission_model,
    _verification_spec,
    question_sha256,
)

logger = logging.getLogger(__name__)


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


def _reuse_near_copies(
    task_id: int, candidate_postings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give a posting its verified twin's verdicts; return the ones still to ask.

    A twin is the same source, title and text once places and numbers are
    removed (core.near_copy). A candidate whose twin already holds closed and
    clearance verdicts takes those and the twin's verdicts under every live
    board and filter prompt, as rows marked verify-near-copy that cost nothing.
    One whose twin is in this sweep or in a parked one waits for it, so the
    text is read once. The posting's own location, date and gates still apply
    where each board and filter reads the verdict.
    """
    near_copy_keys = {
        posting["url"]: near_copy.key(posting["title"] or "", posting["input_content"] or "")
        for posting in candidate_postings
    }
    catalog.set_near_copy_keys(near_copy_keys)
    source_keys = {
        (posting["source"], near_copy_keys[posting["url"]]) for posting in candidate_postings
    }
    sources, digests = (
        [source_key[0] for source_key in source_keys],
        [source_key[1] for source_key in source_keys],
    )
    verified_twins = {
        (row["source"], row["near_copy_key"]): row["url"]
        for row in db.query(
            "SELECT DISTINCT ON (j.source, j.near_copy_key) j.source, j.near_copy_key, j.url "
            "FROM jobs j WHERE (j.source, j.near_copy_key) IN "
            "(SELECT * FROM unnest(%s::text[], %s::text[])) AND NOT (j.url = ANY(%s::text[])) "
            f"AND {verdict_reads.has_verdict('j.url', 'closed')} "
            f"AND {verdict_reads.has_verdict('j.url', 'clearance')} "
            "ORDER BY j.source, j.near_copy_key, j.id DESC",
            (sources, digests, list(near_copy_keys)),
        )
    }
    in_flight_source_keys = {
        (row["source"], row["near_copy_key"])
        for row in db.query(
            "SELECT source, near_copy_key FROM jobs WHERE url = ANY(%s) "
            "AND near_copy_key IS NOT NULL",
            (_in_flight(task_id),),
        )
    }
    postings_to_verify, twin_reuse, selected_source_keys = [], [], set()
    for posting in candidate_postings:
        source_key = (posting["source"], near_copy_keys[posting["url"]])
        if source_key in verified_twins:
            twin_reuse.append((posting, verified_twins[source_key]))
        elif source_key in in_flight_source_keys or source_key in selected_source_keys:
            continue
        else:
            selected_source_keys.add(source_key)
            postings_to_verify.append(posting)
    if twin_reuse:
        _copy_twin_verdicts(twin_reuse)
    return postings_to_verify


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


def candidates_sql() -> str:
    """The verify sweep's candidates, freshest first. Binds
    verification_candidates.params() plus `cap` and `in_flight`.

    The filters run in the order that keeps each one's input small.
    Availability and a missing verdict are one scan of jobs against two
    hashed sets. The gate (verification_candidates.reachable) runs on what
    that leaves, the per-posting probe for page text on what the gate admits,
    and only the capped rows read the text itself. Run as one predicate over
    every posting, with the text read before the sort, the query ran past 14
    minutes on production (2026-10-10) and spilled to disk.
    """
    from api import verification_candidates

    return f"""
    WITH {verification_candidates.TARGETS},
    pool AS MATERIALIZED (
        SELECT j.id, ({verification_candidates.EVERY_TARGET}) AS every_target
        FROM jobs j
        WHERE {catalog.IS_AVAILABLE.format(job="j")}
          AND NOT (j.url = ANY(%(in_flight)s::text[]))
          -- A pause's backlog is not bought when AI comes back (api.ai_pause).
          AND NOT {ai_pause.SKIPPED.format(job="j")} AND (
            NOT {verdict_reads.has_verdict("j.url", "closed")}
            -- Short-circuited pipelines (and any upstream verdict that later
            -- flips to passing) leave downstream checks MISSING, not false;
            -- a job invisible for want of a clearance verdict never heals
            -- unless the sweep looks for holes in every check, not just the
            -- first one.
            OR NOT {verdict_reads.has_verdict("j.url", "clearance")}
        )
    ),
    reached AS MATERIALIZED (
        {verification_candidates.reachable("pool")}
    ),
    chosen AS (
        SELECT r.id, r.date_posted FROM reached r
        -- A scalar subquery, which the planner runs per reached posting. As
        -- a join and an EXISTS it drove the check from all 840,000 fetched
        -- urls instead (60 s on production, 2026-10-10).
        WHERE (
            SELECT EXISTS (
                SELECT 1 FROM page_texts q
                WHERE q.url = j.url AND q.chars > {MIN_CONTENT_CHARS})
            FROM jobs j WHERE j.id = r.id
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
        ORDER BY r.date_posted DESC NULLS LAST
        LIMIT %(cap)s
    )
    SELECT j.url, j.source, j.company, j.title, q.input_content, q.content_ref,
           q.id AS page_fetch_id,
           NOT {verdict_reads.has_verdict("j.url", "closed")} AS needs_closed,
           NOT {verdict_reads.has_verdict("j.url", "clearance")} AS needs_clearance
    FROM chosen c JOIN jobs j ON j.id = c.id
    {CONTENT_LATERAL.format(url="j.url", columns="id, input_content, content_ref")}
    ORDER BY c.date_posted DESC NULLS LAST
    """


def listed_sql() -> str:
    """The named postings still missing a closed or clearance verdict. Binds
    `urls` and `in_flight`.

    For a backfill an administrator asks for by url: the postings the sweep's
    gate (verification_candidates.reachable) and cap held back still have no
    verdict, so nothing records what happened to them. The availability,
    missing-verdict, in-flight and page-text conditions are the sweep's own."""
    return f"""
    SELECT j.url, j.source, j.company, j.title, q.input_content, q.content_ref,
           q.id AS page_fetch_id,
           NOT {verdict_reads.has_verdict("j.url", "closed")} AS needs_closed,
           NOT {verdict_reads.has_verdict("j.url", "clearance")} AS needs_clearance
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id, input_content, content_ref")}
    WHERE j.url = ANY(%(urls)s::text[])
      AND {catalog.IS_AVAILABLE.format(job="j")}
      AND NOT (j.url = ANY(%(in_flight)s::text[]))
      AND (
        NOT {verdict_reads.has_verdict("j.url", "closed")}
        OR NOT {verdict_reads.has_verdict("j.url", "clearance")}
      )
    ORDER BY j.date_posted DESC NULLS LAST
    """


async def handle_verify_new(task_id: int, payload: dict[str, Any]) -> None:
    """Batched replacement for ingest-time closed/clearance checks: one
    half-price call per job yields both verdicts. Idempotent by re-sweep.
    Only successful lines produce verdict rows; anything missed or failed is
    picked up by the next cycle's sweep."""
    specs = []
    if not has_batch_work(task_id):
        from api import verification_candidates

        if "urls" in payload:
            # A requested backfill: the named postings, past the gate and cap.
            rows = db.query(
                listed_sql(), {"urls": payload["urls"], "in_flight": _in_flight(task_id)}
            )
        else:
            candidate_params = {
                **verification_candidates.params(),
                "cap": int(db.get_config("verify_new_per_cycle")),
                "in_flight": _in_flight(task_id),
            }
            # Without JIT. Its cost estimate is in the millions, so JIT compiles
            # the whole plan, and on production (2026-10-10) the same plan took
            # 78 s with it and 24 s without: 38 s went to hashing the
            # 16,000 targets.
            with db.transaction():
                db.execute("SET LOCAL jit = off")
                rows = db.query(candidates_sql(), candidate_params)
        rows = page_fetches.resolve(rows)
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
            managed_boards.verification_questions(rows, model, effort)
            if db.get_config("verify_answers_board_questions")
            else {}
        )
        specs = [
            _joint_spec(r, questions[r["url"]], model, effort)
            if questions.get(r["url"])
            else _verification_spec(
                r["url"],
                r["input_content"],
                {
                    key: r[key]
                    for key in (
                        "company",
                        "title",
                        "needs_closed",
                        "needs_clearance",
                        "page_fetch_id",
                    )
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
                            page_fetch_id=job.get("page_fetch_id"),
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
                        page_fetch_id=job.get("page_fetch_id"),
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
