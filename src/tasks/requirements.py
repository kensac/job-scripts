"""Structured requirements extraction: what a posting actually asks for."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.ai import batch_results
from core import skills as skills_lib
from core.requirements import (
    CLEARANCE_LEVELS,
    DEGREE_LEVELS,
    EMPLOYMENT_TYPES,
    MAX_PLAUSIBLE_YOE,
    REQUIREMENTS_INPUT_CHARS,
    REQUIREMENTS_INSTRUCTIONS,
    SENIORITIES,
    SPONSORSHIPS,
    RequirementsExtract,
    in_vocabulary,
)
from core.shapes import EXTRACT_REQUIREMENTS_PER_CYCLE, REQUIREMENTS_TASK
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL, VERIFIED_OPEN
from tasks import rescrape
from tasks.runtime import (
    consume_result,
    has_batch_work,
    run_batched,
    set_progress,
)

logger = logging.getLogger(__name__)

# Postings never extracted, plus postings whose page has been scraped again
# since they were.
#
# Scoped to postings a person can reach. This sweep started from every url
# with an ai_queries row, so a job kept being re-read for as long as it
# existed, whether or not anyone had enabled its board.
#
# A url with NO job row stays in, and the LEFT JOIN is what keeps it. This
# sweep is url-keyed on purpose: a fifth of the corpus is postings whose job
# row is gone and whose page can never be scraped again, and joining `jobs`
# to reach the gate would have dropped every one of them silently. An orphan
# has no source to judge, so the gate has nothing to say about it.
#
# The change check runs over the whole corpus every cycle, so it must not
# detoast it: the first stage takes only the id of each url's current content
# row, which is an index read, and compares it to the id the stored answer came
# from. Only the survivors of that - and only up to the cap - have their text
# fetched. Getting this the other way round would read 110 MB an hour to learn
# that nothing changed.
#
# `stored_hash` rides along so the handler can tell a re-scrape that changed the
# page from one that did not. An identical re-scrape refreshes the id and pays
# for nothing.
# Invalid stored bounds need fresh extraction even when the source is unchanged.
_INVALID_YEARS = "(r.yoe_min < 0 OR r.yoe_max < 0)"

_CANDIDATES = f"""
    WITH current_row AS (
        SELECT c.url, q.content_row_id
        FROM (
            SELECT DISTINCT a.url FROM ai_queries a
            LEFT JOIN jobs j ON j.url = a.url
            WHERE j.url IS NULL
               OR ({AI_ELIGIBLE_JOB.format(job="j")} AND {VERIFIED_OPEN.format(url="j.url")})
        ) c
        {CONTENT_LATERAL.format(url="c.url", columns="id AS content_row_id")}
    ),
    todo AS (
        SELECT cr.url, cr.content_row_id,
               CASE WHEN {_INVALID_YEARS} THEN NULL ELSE r.content_hash END AS stored_hash
        FROM current_row cr
        LEFT JOIN job_requirements r ON r.url = cr.url
        WHERE r.url IS NULL
           OR {_INVALID_YEARS}
           OR r.content_row_id IS DISTINCT FROM cr.content_row_id
        LIMIT %(cap)s
    )
    SELECT t.url, t.content_row_id, t.stored_hash, q.input_content
    FROM todo t
    {CONTENT_LATERAL.format(url="t.url", columns="input_content")}
"""


def _years(parsed: RequirementsExtract) -> tuple[int | None, int | None]:
    """Years of experience as a (floor, ceiling) pair.

    A max with no min is what a posting means by "0-3 years" or "up to 3
    years", and the model returns it that way often enough to matter; the floor
    is zero, and leaving it NULL would hide the posting from every "roles I
    qualify for" filter that compares against yoe_min.
    """
    low, high = parsed.yoe_min, parsed.yoe_max
    invalid_low = low is not None and low < 0
    if invalid_low:
        low = None
    if high is not None and high < 0:
        high = None
    if low is None and high is None:
        return None, None
    if low is None and not invalid_low:
        low = 0
    if low is not None and high is not None and high < low:
        low, high = high, low
    # A posting asking for more than a career's worth of experience is a parse
    # slip (a year, a salary, a requisition number), and a wrong number here
    # silently reorders every "what does this market want" answer.
    if low is not None and low > MAX_PLAUSIBLE_YOE:
        return None, None
    if high is not None and high > MAX_PLAUSIBLE_YOE:
        high = None
    return low, high


def _store(
    url: str,
    parsed: RequirementsExtract,
    content_hash: str,
    content_row_id: int | None,
    model: str | None = None,
) -> None:
    yoe_min, yoe_max = _years(parsed)
    stated = parsed.has_requirements
    with db.transaction():
        db.execute(
            """
            INSERT INTO job_requirements (
                url, has_requirements, yoe_min, yoe_max, degree_min, degree_required,
                degree_fields, enrollment_required, seniority, employment_type,
                clearance, citizenship_required, sponsorship, model, content_hash,
                content_row_id)
            VALUES (%(url)s, %(has)s, %(ymin)s, %(ymax)s, %(deg)s, %(degreq)s,
                    %(fields)s, %(enrol)s, %(sen)s, %(emp)s, %(clr)s, %(cit)s,
                    %(spon)s, %(model)s, %(hash)s, %(row_id)s)
            ON CONFLICT (url) DO UPDATE SET
                has_requirements = EXCLUDED.has_requirements,
                yoe_min = EXCLUDED.yoe_min, yoe_max = EXCLUDED.yoe_max,
                degree_min = EXCLUDED.degree_min,
                degree_required = EXCLUDED.degree_required,
                degree_fields = EXCLUDED.degree_fields,
                enrollment_required = EXCLUDED.enrollment_required,
                seniority = EXCLUDED.seniority,
                employment_type = EXCLUDED.employment_type,
                clearance = EXCLUDED.clearance,
                citizenship_required = EXCLUDED.citizenship_required,
                sponsorship = EXCLUDED.sponsorship,
                model = EXCLUDED.model, content_hash = EXCLUDED.content_hash,
                content_row_id = EXCLUDED.content_row_id,
                extracted_at = now()
            """,
            {
                "url": url,
                "has": stated,
                "ymin": yoe_min if stated else None,
                "ymax": yoe_max if stated else None,
                "deg": in_vocabulary(parsed.degree_min, DEGREE_LEVELS) if stated else None,
                "degreq": bool(parsed.degree_required) and stated,
                "fields": [f.strip() for f in parsed.degree_fields if f.strip()] if stated else [],
                "enrol": bool(parsed.enrollment_required) and stated,
                "sen": in_vocabulary(parsed.seniority, SENIORITIES) if stated else None,
                "emp": in_vocabulary(parsed.employment_type, EMPLOYMENT_TYPES) if stated else None,
                "clr": in_vocabulary(parsed.clearance, CLEARANCE_LEVELS) if stated else None,
                "cit": bool(parsed.citizenship_required) and stated,
                "spon": in_vocabulary(parsed.sponsorship, SPONSORSHIPS) if stated else None,
                "model": model,
                "hash": content_hash,
                "row_id": content_row_id,
            },
        )
        # Replaced wholesale rather than merged: a re-extraction that drops a
        # skill means the posting no longer asks for it, and a left-over row
        # would keep answering the market query with a requirement that is gone.
        db.execute("DELETE FROM job_skills WHERE url = %s", (url,))
        rows = []
        if stated:
            for kind, raw_list in (
                ("required", parsed.skills_required),
                ("preferred", parsed.skills_preferred),
            ):
                for raw in raw_list:
                    skill = skills_lib.canonical(raw)
                    if skill:
                        rows.append((url, kind, skill, raw.strip()))
        if rows:
            # A posting can write the same skill twice ("Python", "python");
            # both collapse onto one canonical row, and the primary key is on
            # the raw text, so the duplicate has to be dropped here.
            db.executemany(
                "INSERT INTO job_skills (url, kind, skill, skill_raw) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (url, kind, skill_raw) DO NOTHING",
                rows,
            )


async def handle_extract_requirements(task_id: int, payload: dict[str, Any]) -> None:
    from core.batch import structured_response_spec

    resumed = has_batch_work(task_id)
    rows = [] if resumed else db.query(_CANDIDATES, {"cap": EXTRACT_REQUIREMENTS_PER_CYCLE})
    rows = rescrape.drop_unchanged(rows, table="job_requirements", limit=REQUIREMENTS_INPUT_CHARS)
    if not rows and not resumed:
        set_progress(task_id, 0, 0, "nothing to extract")
        return
    specs = [
        structured_response_spec(
            r["url"],
            REQUIREMENTS_INSTRUCTIONS,
            r["input_content"][:REQUIREMENTS_INPUT_CHARS],
            RequirementsExtract,
            context={"content_hash": r["content_hash"], "content_row_id": r["content_row_id"]},
        )
        for r in rows
    ]
    set_progress(task_id, 0, len(specs), "requirements batch")
    results, _ = await run_batched(task_id, REQUIREMENTS_TASK, specs)
    done = 0
    for res in results:
        url = res.custom_id
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            context = res.request.context if res.request else None
            if not context or not context.get("content_hash") or not context.get("content_row_id"):
                receipt.outcome = "unknown_request"
                continue
            if not rescrape.content_is_current(url, context["content_row_id"]):
                receipt.outcome = "superseded"
                continue
            if res.error or not res.text:
                receipt.outcome = "failed"
                continue
            try:
                parsed = RequirementsExtract.model_validate_json(res.text)
            except ValueError:
                logger.warning("requirements parse failed for %s", url)
                receipt.outcome = "failed"
                continue
            _store(url, parsed, context["content_hash"], context["content_row_id"], res.model)
            receipt.outcome = "written"
            done += 1
        if done % 200 == 0:
            set_progress(task_id, *batch_results.progress_counts(task_id), "requirements extracted")
    set_progress(task_id, *batch_results.progress_counts(task_id), "requirements extracted")
