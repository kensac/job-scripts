"""Compensation extraction, normalised to a yearly figure."""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel

from api import ai, db
from api.tasks.runtime import (
    _batch_event_hook,
    _pending_batch_ids,
    _set_progress,
    submit_or_collect,
)
from core.store import CONTENT_LATERAL

logger = logging.getLogger("jobtracker_worker")


# Comp extraction runs hourly and each pass is bounded, so one task cannot pull
# the whole catalog into memory or occupy a worker indefinitely. The size is
# chosen to fill the batch-wave concurrency rather than picked arbitrarily: a
# comp spec is ~6.5k tokens against a 1.8M-token wave budget, so ~276 specs per
# wave, and waves now run BATCH_WAVE_CONCURRENCY at a time.
EXTRACT_COMP_PER_CYCLE = int(os.environ.get("JOBTRACKER_EXTRACT_COMP_PER_CYCLE", "1100"))


# Multipliers to a yearly figure. The old version knew only hourly and
# monthly, so a weekly wage was either stored raw ($5,000/week became
# $5,000/yr) or multiplied by 2080 ($2,000/week became $4,160,000/yr). Both
# shapes are in production data today, which is what makes the comp column
# unsortable. "one_time" is deliberately absent: a stipend or signing bonus
# has no annual equivalent and must not be invented.
_PERIOD_TO_YEARLY = {
    "hourly": 2080.0,
    "daily": 260.0,
    "weekly": 52.0,
    "biweekly": 26.0,
    "semimonthly": 24.0,
    "monthly": 12.0,
    "yearly": 1.0,
}


COMP_PERIODS = (*tuple(_PERIOD_TO_YEARLY), "one_time")


COMP_BASES = ("base", "total", "stipend", "unspecified")


class CompExtract(BaseModel):
    """Standardised so the number is comparable across postings. Amounts stay
    exactly as advertised; normalisation to a yearly figure happens here, not
    in the model, so a bad period can be corrected without re-running the AI."""

    has_comp: bool
    comp_min: float | None = None
    comp_max: float | None = None
    currency: str = ""
    period: str = ""
    basis: str = ""
    display: str = ""


_COMP_INSTRUCTIONS = (
    "Extract the advertised compensation for THIS job from the page content. "
    "has_comp=true only when a concrete pay amount or range is stated for this "
    "role; false for benefits, equity-only mentions, and salary-law boilerplate "
    "with no numbers.\n"
    "comp_min/comp_max: numeric bounds EXACTLY as advertised, never converted "
    "(26.44 for $26.44/hr, 120000 for $120k/yr, 2000 for $2,000 per week). "
    "Equal values when a single amount is given.\n"
    "period: EXACTLY one of hourly, daily, weekly, biweekly, semimonthly, "
    "monthly, yearly, one_time. Read it from the posting - do not guess from "
    "the size of the number. Use one_time for a stipend, signing bonus, or any "
    "lump sum that is not a recurring wage.\n"
    "basis: base for salary only, total for explicit total compensation or OTE, "
    "stipend for an internship or one-off stipend, unspecified if unclear.\n"
    "currency: ISO 4217 code, e.g. USD, CAD, GBP. Use USD only when the posting "
    "actually indicates US dollars.\n"
    "display: a compact human string as advertised, e.g. '$120k-$150k' or '$45/hr'."
)


def _annualize(value: float | None, period: str) -> int | None:
    """Yearly equivalent of an advertised amount, or None when there isn't one.

    None is the right answer more often than a number: an unrecognised period,
    or a one-off payment, has no annual equivalent, and a wrong sortable value
    is worse than a missing one because it silently reorders the column.
    """
    if value is None:
        return None
    multiplier = _PERIOD_TO_YEARLY.get((period or "").strip().lower())
    if multiplier is None:
        return None
    annual = round(value * multiplier)
    # Model slips (cents-as-ints, stray digits) produce absurd annuals;
    # better no number than a wrong sortable one — display text is kept.
    if annual < 5_000 or annual > 5_000_000:
        return None
    return annual


async def handle_extract_comp(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    rows = db.query(
        f"""
        SELECT j.id, j.url, q.input_content
        FROM jobs j
        {CONTENT_LATERAL.format(url="j.url")}
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
        results = await submit_or_collect(
            task_id, specs, ai.DEFAULT_MODELS["openai"], "low", 1500, hook
        )
    done = 0
    for url, res in results.items():
        job_id = by_url.get(url)
        if job_id is None:
            continue
        comp_min = comp_max = None
        comp_text = comp_period = comp_currency = comp_basis = None
        parsed_ok = False
        if res.text and not res.error:
            try:
                parsed = CompExtract.model_validate_json(res.text)
                parsed_ok = True
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
            except Exception:
                logger.warning(f"comp parse failed for {url}")
        if parsed_ok:
            db.execute(
                "UPDATE jobs SET comp_min = %s, comp_max = %s, comp_text = %s, "
                "comp_period = %s, comp_currency = %s, comp_basis = %s, "
                "comp_extracted = TRUE WHERE id = %s",
                (comp_min, comp_max, comp_text, comp_period, comp_currency, comp_basis, job_id),
            )
        # Failed/errored lines stay comp_extracted=false so the next daily
        # sweep retries them — batch operations are idempotent by re-sweep.
        done += 1
        if done % 200 == 0:
            _set_progress(task_id, done, len(specs), "comp extracted")
    _set_progress(task_id, done, len(specs), "comp extracted")
