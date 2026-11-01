"""Verify new postings and reuse verified near copies where possible."""

from __future__ import annotations

import json
import logging
from typing import Any

from api import ai, db, managed_boards
from api.ai import verdicts
from api.ai.batch_results import progress_counts
from api.verdict_equivalence import answering_models
from core import page_fetches
from core.answers import VERIFICATION_REQUEST, FilterDecision, joint_question_key
from core.batch import BatchResult, BatchSpec
from core.shapes import VERIFY_TASK
from core.store import decided_custom_urls
from tasks.runtime import consume_result, has_batch_work, run_batched, set_progress
from tasks.verification.near_copies import _reuse_near_copies
from tasks.verification.questions import (
    _joint_spec,
    _row_context,
    _submission_model,
    _verification_spec,
    question_sha256,
)
from tasks.verification.selection import _select_postings

logger = logging.getLogger(__name__)


async def handle_verify_new(task_id: int, payload: dict[str, Any]) -> None:
    """Batched replacement for ingest-time closed/clearance checks: one
    half-price call per job yields both verdicts. Idempotent by re-sweep.
    Only successful lines produce verdict rows; anything missed or failed is
    picked up by the next cycle's sweep."""
    specs = []
    if not has_batch_work(task_id):
        rows = page_fetches.resolve(_select_postings(task_id, payload))
        if not rows:
            set_progress(task_id, 0, 0, "nothing to verify")
            return
        if db.get_config("verify_near_copy_reuse"):
            rows = _reuse_near_copies(task_id, rows)
            if not rows:
                set_progress(task_id, 0, 0, "every candidate reused a twin's verdicts")
                return
        specs = _build_specs(rows)
        set_progress(task_id, 0, len(specs), "verify batch submitted")
    results, _ = await run_batched(task_id, VERIFY_TASK, specs)
    for res in results:
        with consume_result(task_id, res) as receipt:
            if receipt.pending:
                receipt.outcome = _record_result(res)
    done, total = progress_counts(task_id)
    set_progress(task_id, done, total, "verified")


def _build_specs(rows: list[dict[str, Any]]) -> list[BatchSpec]:
    """One request per posting: joint with the boards' questions where a
    board asks one, verification alone otherwise."""
    model, effort = _submission_model()
    questions = (
        managed_boards.verification_questions(rows, model, effort)
        if db.get_config("verify_answers_board_questions")
        else {}
    )
    return [
        _joint_spec(r, questions[r["url"]], model, effort)
        if questions.get(r["url"])
        else _verification_spec(r["url"], r["input_content"], _row_context(r))
        for r in rows
    ]


def _record_result(res: BatchResult) -> str:
    """Write the verdicts one collected result settles; return its receipt outcome."""
    job = res.request.context if res.request else None
    if not job:
        return "unknown_request"
    if res.error or not res.text:
        return "failed"
    boards = job.get("boards") or []
    decisions: dict[int, FilterDecision] = {}
    try:
        if boards:
            answer = json.loads(res.text)
            parsed = VERIFICATION_REQUEST.response_model.model_validate(answer["verification"])
            decisions = {
                board["board_id"]: FilterDecision.model_validate(
                    answer[joint_question_key(board["board_id"])]
                )
                for board in boards
            }
        else:
            parsed = VERIFICATION_REQUEST.response_model.model_validate_json(res.text)
    except ValueError, KeyError, TypeError:
        logger.warning("verify_new: unparsable batch output for %s", res.custom_id)
        return "invalid_output"
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
            [res.custom_id],
            board["prompt_hash"],
            answering_models("custom", board["model"]),
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
    return "written" if written else "superseded"
