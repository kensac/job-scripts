"""Compensation extraction, normalised to a yearly figure."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.ai import batch_results
from core.comp import (
    COMP_BASES,
    COMP_INPUT_CHARS,
    COMP_INSTRUCTIONS,
    COMP_PERIODS,
    PERIOD_TO_YEARLY,
    CompExtract,
)
from core.shapes import COMP_TASK, EXTRACT_COMP_PER_CYCLE
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL, VERIFIED_OPEN
from tasks import rescrape
from tasks.runtime import (
    consume_result,
    has_batch_work,
    run_batched,
    set_progress,
)

logger = logging.getLogger(__name__)


def _annualize(value: float | None, period: str) -> int | None:
    """Yearly equivalent of an advertised amount, or None when there isn't one.

    None is the right answer more often than a number: an unrecognised period,
    or a one-off payment, has no annual equivalent, and a wrong sortable value
    is worse than a missing one because it silently reorders the column.
    """
    if value is None:
        return None
    multiplier = PERIOD_TO_YEARLY.get((period or "").strip().lower())
    if multiplier is None:
        return None
    annual = round(value * multiplier)
    # Model slips (cents-as-ints, stray digits) produce absurd annuals;
    # better no number than a wrong sortable one. Display text is kept.
    if annual < 5_000 or annual > 5_000_000:
        return None
    return annual


async def handle_extract_comp(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    resumed = has_batch_work(task_id)
    rows = (
        []
        if resumed
        else db.query(
            f"""
        SELECT j.id, j.url, q.input_content, q.id AS content_row_id
        FROM jobs j
        {CONTENT_LATERAL.format(url="j.url", columns="id, input_content")}
        WHERE (NOT j.comp_extracted
               OR (j.comp_content_row_id IS NOT NULL AND j.comp_content_row_id <> q.id)
               OR (j.comp_period IS NULL AND (j.comp_min IS NOT NULL OR j.comp_max IS NOT NULL)))
          AND j.active
          AND {AI_ELIGIBLE_JOB.format(job="j")}
          AND {VERIFIED_OPEN.format(url="j.url")}
        ORDER BY j.id DESC
        LIMIT %(cap)s
        """,
            {"cap": EXTRACT_COMP_PER_CYCLE},
        )
    )
    if not rows and not resumed:
        set_progress(task_id, 0, 0, "nothing to extract")
        return
    schema = to_strict_json_schema(CompExtract)
    specs = [
        BatchSpec(
            r["url"],
            COMP_INSTRUCTIONS,
            r["input_content"][:COMP_INPUT_CHARS],
            "CompExtract",
            schema,
            context={"job_id": r["id"], "content_row_id": r["content_row_id"]},
        )
        for r in rows
    ]
    set_progress(task_id, 0, len(specs), "comp batch submitted (half price)")
    results, _ = await run_batched(task_id, COMP_TASK, specs)
    done = 0
    for res in results:
        url = res.custom_id
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            context = res.request.context if res.request else None
            if not context or not context.get("job_id") or not context.get("content_row_id"):
                receipt.outcome = "unknown_request"
                continue
            if not rescrape.content_is_current(url, context["content_row_id"]):
                receipt.outcome = "superseded"
                continue
            job_id = context["job_id"]
            comp_min = comp_max = None
            comp_text = comp_period = comp_currency = comp_basis = None
            parsed_ok = False
            written = 0
            if res.text and not res.error:
                try:
                    parsed = CompExtract.model_validate_json(res.text)
                    if parsed.has_comp:
                        period = (parsed.period or "").strip().lower()
                        comp_min = _annualize(parsed.comp_min, period)
                        comp_max = _annualize(parsed.comp_max, period) or comp_min
                        if comp_min and comp_max and comp_min > comp_max:
                            comp_min, comp_max = comp_max, comp_min
                        comp_text = parsed.display or None
                        # Kept so the annual figure can be re-derived, and so the
                        # UI can say what it is looking at. A yearly number with no
                        # period or currency beside it cannot be audited.
                        comp_period = period if period in COMP_PERIODS else None
                        comp_currency = (parsed.currency or "").strip().upper()[:3] or None
                        basis = (parsed.basis or "").strip().lower()
                        comp_basis = basis if basis in COMP_BASES else None
                    parsed_ok = True
                except ValueError:
                    logger.warning(f"comp parse failed for {url}")
            if parsed_ok:
                written = db.execute_count(
                    "UPDATE jobs j SET comp_min = %s, comp_max = %s, comp_text = %s, "
                    "comp_period = %s, comp_currency = %s, comp_basis = %s, "
                    "comp_extracted = TRUE, comp_content_row_id = %s "
                    "FROM (VALUES (%s::text)) AS page(url) "
                    + CONTENT_LATERAL.format(url="page.url", columns="id")
                    + " WHERE j.id = %s AND j.url = page.url AND q.id = %s",
                    (
                        comp_min,
                        comp_max,
                        comp_text,
                        comp_period,
                        comp_currency,
                        comp_basis,
                        context["content_row_id"],
                        url,
                        job_id,
                        context["content_row_id"],
                    ),
                )
            # The 2026-09-05 audit found unconditional progress accounting
            # could report done == total even when every line failed. Count
            # writes, not parsed or collected responses; failed lines retain
            # prior values and the selection predicates govern their retry.
            receipt.outcome = (
                "written"
                if parsed_ok and written
                else "failed"
                if not parsed_ok
                else "superseded"
                if db.query_one("SELECT id FROM jobs WHERE id = %s", (job_id,))
                else "missing_subject"
            )
            done += int(parsed_ok and bool(written))
        if done % 200 == 0:
            set_progress(task_id, *batch_results.progress_counts(task_id), "comp extracted")
    set_progress(task_id, *batch_results.progress_counts(task_id), "comp extracted")
