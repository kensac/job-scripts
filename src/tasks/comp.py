"""Compensation extraction, normalised to a yearly figure."""

from __future__ import annotations

from typing import Any

from api import compensation_candidates, db
from core import catalog, verdict_reads
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


# A stored amount with no period was annualised by an older rule and is read
# again even when the page is unchanged, so its hash does not count.
_UNREADABLE = "(c.comp_period IS NULL AND (c.comp_min IS NOT NULL OR c.comp_max IS NOT NULL))"


def _select(cap: int, payload: dict[str, Any]) -> list[Row]:
    # Never read, or read from a page fetch that is no longer current. An
    # answer with no recorded fetch (62,004 from before fetches were recorded)
    # is kept. `stored_hash` lets the sweep re-stamp a re-fetch that changed
    # nothing instead of paying for it again.
    cte, eligible = compensation_candidates.selection(
        bool(db.get_config("compensation_demand_gate_enabled"))
    )
    return db.query(
        f"""
        {cte}
        SELECT j.id, j.url, q.input_content, q.id AS content_row_id,
               CASE WHEN {_UNREADABLE} THEN NULL ELSE c.content_hash END AS stored_hash
        FROM jobs j
        {CONTENT_LATERAL.format(url="j.url", columns="id, input_content")}
        LEFT JOIN job_comp c ON c.url = j.url
        WHERE (c.url IS NULL
               OR (c.content_row_id IS NOT NULL AND c.content_row_id <> q.id)
               OR {_UNREADABLE})
          AND {catalog.IS_AVAILABLE.format(job="j")}
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
    values = {
        "url": result.custom_id,
        "min": comp_min,
        "max": comp_max,
        "text": comp_text,
        "period": comp_period,
        "currency": comp_currency,
        "basis": comp_basis,
        "model": result.model,
        "row": context["content_row_id"],
        "chars": COMP_INPUT_CHARS,
    }
    # The guard is in the statement: it reads the current page row again, so
    # a page fetched after the currency check leaves the answer unwritten. The
    # hash is of the text the model read, so an unchanged re-fetch can be told
    # from a changed one.
    written = db.execute_count(_STORE, values)
    # The 2026-09-05 audit found unconditional progress accounting could
    # report done == total even when every line failed. Count writes, not
    # parsed or collected responses; failed lines retain prior values and the
    # selection predicates govern their retry.
    return "written" if written else "superseded"


_STORE = (
    """
    INSERT INTO job_comp (url, comp_min, comp_max, comp_text, comp_period, comp_currency,
                          comp_basis, model, content_hash, content_row_id)
    SELECT page.url, %(min)s, %(max)s, %(text)s, %(period)s, %(currency)s, %(basis)s,
           %(model)s,
           encode(sha256(convert_to(left(q.input_content, %(chars)s), 'UTF8')), 'hex'), q.id
    FROM (VALUES (%(url)s::text)) AS page(url)
    """
    + CONTENT_LATERAL.format(url="page.url", columns="id, input_content")
    + """
    WHERE q.id = %(row)s
    ON CONFLICT (url) DO UPDATE SET
        comp_min = EXCLUDED.comp_min, comp_max = EXCLUDED.comp_max,
        comp_text = EXCLUDED.comp_text, comp_period = EXCLUDED.comp_period,
        comp_currency = EXCLUDED.comp_currency, comp_basis = EXCLUDED.comp_basis,
        model = EXCLUDED.model, content_hash = EXCLUDED.content_hash,
        content_row_id = EXCLUDED.content_row_id, extracted_at = now()
    """
)


PAY = Derivation(
    kind="extract_comp",
    purpose=COMP_TASK.purpose,
    noun="comp",
    table="job_comp",
    per_cycle_key="comp_extract_per_cycle",
    select=_select,
    requests=_requests,
    store=_store,
    input_chars=COMP_INPUT_CHARS,
    recipe=None,
    shape=COMP_TASK,
    answer=CompExtract,
    context_keys=("job_id", "content_row_id"),
    skip_unchanged=True,
)
