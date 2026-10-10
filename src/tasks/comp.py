"""Compensation extraction, normalised to a yearly figure."""

from __future__ import annotations

from typing import Any

from api import compensation_candidates, db
from core import verdict_reads
from core.batch import BatchResult, BatchSpec, structured_response_spec
from core.comp import (
    COMP_BASES,
    COMP_INPUT_CHARS,
    COMP_INSTRUCTIONS,
    COMP_PERIODS,
    PERIOD_TO_YEARLY,
    CompExtract,
)
from core.shapes import COMP_TASK
from core.store import CONTENT_LATERAL
from tasks.derive import Derivation, Row


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


def _select(cap: int, payload: dict[str, Any]) -> list[Row]:
    cte, eligible = compensation_candidates.selection(
        bool(db.get_config("compensation_demand_gate_enabled"))
    )
    return db.query(
        f"""
        {cte}
        SELECT j.id, j.url, q.input_content, q.id AS content_row_id
        FROM jobs j
        {CONTENT_LATERAL.format(url="j.url", columns="id, input_content")}
        WHERE (NOT j.comp_extracted
               OR (j.comp_content_row_id IS NOT NULL AND j.comp_content_row_id <> q.id)
               OR (j.comp_period IS NULL AND (j.comp_min IS NOT NULL OR j.comp_max IS NOT NULL)))
          AND j.active
          AND {eligible}
          AND {verdict_reads.verified_open("j.url")}
        ORDER BY j.id DESC
        LIMIT %(cap)s
        """,
        {"cap": cap},
    )


def _requests(rows: list[Row]) -> list[BatchSpec]:
    return [
        structured_response_spec(
            r["url"],
            COMP_INSTRUCTIONS,
            r["input_content"][:COMP_INPUT_CHARS],
            CompExtract,
            context={"job_id": r["id"], "content_row_id": r["content_row_id"]},
        )
        for r in rows
    ]


def _store(result: BatchResult, context: dict[str, Any], parsed: CompExtract) -> str:
    comp_min = comp_max = None
    comp_text = comp_period = comp_currency = comp_basis = None
    if parsed.has_comp:
        period = (parsed.period or "").strip().lower()
        comp_min = _annualize(parsed.comp_min, period)
        comp_max = _annualize(parsed.comp_max, period) or comp_min
        if comp_min and comp_max and comp_min > comp_max:
            comp_min, comp_max = comp_max, comp_min
        comp_text = parsed.display or None
        # Kept so the annual figure can be re-derived, and so the UI can say
        # what it is looking at. A yearly number with no period or currency
        # beside it cannot be audited.
        comp_period = period if period in COMP_PERIODS else None
        comp_currency = (parsed.currency or "").strip().upper()[:3] or None
        basis = (parsed.basis or "").strip().lower()
        comp_basis = basis if basis in COMP_BASES else None
    # The write re-reads the current page row in the same statement, so a page
    # fetched after the currency check leaves the answer unwritten.
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
            result.custom_id,
            context["job_id"],
            context["content_row_id"],
        ),
    )
    # The 2026-09-05 audit found unconditional progress accounting could
    # report done == total even when every line failed. Count writes, not
    # parsed or collected responses; failed lines retain prior values and the
    # selection predicates govern their retry.
    if written:
        return "written"
    if db.query_one("SELECT id FROM jobs WHERE id = %s", (context["job_id"],)):
        return "superseded"
    return "missing_subject"


PAY = Derivation(
    kind="extract_comp",
    purpose=COMP_TASK.purpose,
    noun="comp",
    table="jobs",
    per_cycle_key="comp_extract_per_cycle",
    select=_select,
    requests=_requests,
    store=_store,
    input_chars=COMP_INPUT_CHARS,
    recipe=None,
    shape=COMP_TASK,
    answer=CompExtract,
    context_keys=("job_id", "content_row_id"),
)
