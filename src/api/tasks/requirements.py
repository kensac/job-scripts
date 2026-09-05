"""Structured requirements extraction: what a posting actually asks for."""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
from typing import Any

from pydantic import BaseModel

from api import db
from api.tasks.runtime import (
    _set_progress,
    run_batched,
)
from core import skills as skills_lib
from core.providers import StructuredOutput
from core.requirements import (
    CLEARANCE_LEVELS,
    DEGREE_LEVELS,
    EMPLOYMENT_TYPES,
    MAX_PLAUSIBLE_YOE,
    SENIORITIES,
    SPONSORSHIPS,
    in_vocabulary,
)
from core.routing import Evidence, TaskShape
from core.store import AI_ELIGIBLE_JOB, CONTENT_LATERAL

logger = logging.getLogger("jobtracker_worker")


# Postings are truncated before they reach the model. 20k characters covers all
# but 24 of the 20,730 pages in the corpus, and the tail past that is boilerplate
# (similar-role lists, cookie notices) rather than requirements.
REQUIREMENTS_INPUT_CHARS = 20000


# gpt-5-nano is the fleet default and is the wrong model here, so this pass
# names its own. Audited over 60 real postings against whether the page even
# mentions the fact: nano at "low" effort invented a clearance level for 12 of
# 55 postings that never mention clearance, and lost 5 of 60 responses to the
# output cap; nano at "minimal" filled 0 and "none" wherever the honest answer
# was "unstated", which is the one distinction this schema exists to keep.
# gpt-5-mini at "minimal" invented one degree, one yoe and no clearances, and
# truncated nothing. Batched over the whole corpus that is $9.89 against nano's
# $1.98 - a one-time pass over postings that can never be re-scraped.
REQUIREMENTS_MODEL = os.environ.get("JOBTRACKER_REQUIREMENTS_MODEL", "gpt-5.6-luna")


# "minimal" is not a cost compromise, it is the better answer: low-effort
# reasoning was 78% of the output bill (802 output tokens per posting against
# 194) and bought nothing this task needs, since every field is copied off the
# page rather than deduced.
REQUIREMENTS_REASONING_EFFORT = "minimal"

# Cheapest first, and every value here is accepted by SOME model in the
# registry, so swapping the model swaps the effort with it rather than sending
# one the model refuses. luna takes "none"; nano takes "minimal", and sent
# the same request directly on 2026-09-05 it completed at ~200 output tokens
# with no reasoning, against 750 to 1,450 at "low". The 21,525-line failure
# of 2026-09-04 was not the effort: OpenAI's error file for those batches says
# every line carried "none", which nano rejects, from a routing path that did
# not yet re-pick the effort for an overridden model.
_EFFORT_PREFERENCE = ("none", "minimal", "low")


# Measured p100 over the pilot was 378 tokens; the JSON's whole variable part is
# the two skill arrays, so the budget is set to carry roughly three times the
# widest skill list seen. An unfinished response is unparseable JSON, and an
# unparseable line leaves the url unextracted for the next sweep to re-pay
# forever, so the headroom is worth more than the tokens - a cap is a ceiling,
# not a charge. 2000 rather than 1000 because nano at low effort spent 802
# output tokens a posting in the pilot before the JSON, and a cap the
# reasoning alone can hit turns every long posting into an unparseable line.
REQUIREMENTS_MAX_OUTPUT_TOKENS = 2000


# The same declaration every other batched extraction makes. The model is the
# caller's judgment - the note above is why mini and not nano - and the router
# checks it can do what this task needs rather than choosing it for cost.
# est_prompt_tokens is the fitted figure from the pilot below, not the
# conservative chunking estimate: it ranks candidates and never becomes a bill.


# Bounded per cycle for the same reason comp extraction is: one task must not
# pull the whole corpus into memory or hold a worker indefinitely. Sized to fill
# the batch-wave concurrency rather than picked round, using core.batch's own
# estimator since that is what actually chunks the waves: instructions (3,611
# chars) plus a posting (5,608 chars, the corpus mean after truncation) is 2,302
# tokens at BATCH_CHARS_PER_TOKEN, plus REQUIREMENTS_MAX_OUTPUT_TOKENS reserved,
# so 3,304 tokens a spec against the 1.8M-token BATCH_TOKEN_BUDGET is 545 specs
# per wave, and waves run BATCH_WAVE_CONCURRENCY (4) at a time: 2,179.
#
# That estimate is deliberately conservative, and it is worth knowing by how
# much. Fitted against real API usage over a 60-posting pilot, the true cost of
# a request is 1,255 + 0.181 x content_chars input tokens: job postings tokenize
# at about 5.5 characters per token, not the 4 that BATCH_CHARS_PER_TOKEN
# assumes, and the 1,255 fixed is the instructions plus the JSON schema, which
# is roughly 45% of a request's input at this posting length. So real waves run
# under budget rather than over it, which is the safe direction. Whole corpus:
# 47.0M input and 4.0M output tokens, $9.89 batched at REQUIREMENTS_MODEL.
EXTRACT_REQUIREMENTS_PER_CYCLE = int(
    os.environ.get("JOBTRACKER_EXTRACT_REQUIREMENTS_PER_CYCLE", "2179")
)


REQUIREMENTS_TASK = TaskShape(
    purpose="requirements",
    label="Requirements extraction",
    per_cycle=EXTRACT_REQUIREMENTS_PER_CYCLE,
    evidence=(
        Evidence(
            model="gpt-5-nano",
            verdict="excluded",
            finding=(
                "Invented a clearance level for 12 of 55 postings whose page "
                "never mentions clearance, and at minimal effort filled 0 and "
                "'none' wherever the honest answer was 'unstated' - which is "
                "the distinction this extraction exists to keep."
            ),
            sample_size=60,
            measured_on=datetime.date(2026, 9, 2),
        ),
        Evidence(
            model="gpt-5-mini",
            verdict="excluded",
            finding=(
                "Extracts well - over the same postings it invented one "
                "degree, one years-of-experience figure and no clearances, "
                "and truncated nothing. Excluded on RELIABILITY rather than "
                "quality: 499 of its 31,999 batched requests failed, against "
                "0 of 19,971 on nano and 0 of 60,000 on luna. A failed line "
                "leaves a posting unextracted and looks like a batch that "
                "worked, so nothing was watching."
            ),
            sample_size=31999,
            measured_on=datetime.date(2026, 9, 2),
        ),
        Evidence(
            model="gpt-5.6-luna",
            verdict="chosen",
            finding=(
                "Zero failures across 60,000 batched requests, the only model "
                "with a clean record at that volume. Chosen for reliability; "
                "its extraction quality on this task has NOT been audited the "
                "way nano and mini were, and that gap is the reason this entry "
                "exists rather than a note."
            ),
            sample_size=60000,
            measured_on=datetime.date(2026, 9, 2),
        ),
    ),
    notes=(
        "Not nano, on measured quality: over 60 real postings it invented a "
        "clearance level for 12 of 55 that never mention clearance, and at "
        "minimal effort filled 0 and 'none' wherever the honest answer was "
        "'unstated' - the one distinction this extraction exists to keep. "
        "Not mini, on measured RELIABILITY: 499 of 31,999 batched requests "
        "failed against zero on the other two, and a failed line leaves a "
        "posting unextracted while looking like a batch that worked. luna is "
        "the only model with a clean record at volume, and its extraction "
        "quality here has not been audited the way the other two were."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=REQUIREMENTS_MAX_OUTPUT_TOKENS,
    est_prompt_tokens=2270,
    # Preference, not a pin. luna REJECTS "minimal" and mini rejects "none",
    # so a literal effort makes the model unswappable: point this at the other
    # one and resolve() refuses, or worse a batch submits and fails whole on a
    # 400. That is #179 exactly. The model picks the first value it accepts.
    effort_preference=_EFFORT_PREFERENCE,
    candidates=(REQUIREMENTS_MODEL,),
)


class RequirementsExtract(BaseModel):
    """What the posting STATES, not what the role probably wants.

    Every field is optional-by-omission because absence is the most common
    answer in this corpus and is itself the finding: a market where 60% of
    postings never name a degree is a different market from one where they all
    demand a bachelor's, and a schema that cannot say "unstated" collapses the
    two. Values are validated against the vocabularies above in Python rather
    than pinned by the JSON schema, so a drifting model answer can be corrected
    without re-running a paid pass.
    """

    has_requirements: bool
    yoe_min: int | None = None
    yoe_max: int | None = None
    degree_min: str = ""
    degree_required: bool = False
    degree_fields: list[str] = []
    enrollment_required: bool = False
    seniority: str = ""
    employment_type: str = ""
    clearance: str = ""
    citizenship_required: bool = False
    sponsorship: str = ""
    skills_required: list[str] = []
    skills_preferred: list[str] = []


_REQUIREMENTS_INSTRUCTIONS = (
    "Extract what THIS job posting requires of a candidate. Report only what the "
    "employer states about this role. The page may also carry aggregator "
    "commentary, company news, funding history, other applicants, benefits, "
    "'similar jobs' listings and an application form: none of that is a "
    "requirement of this role.\n"
    "has_requirements: true only when the page states qualifications for this "
    "role. False for a bare application form, a login wall, an error page, or a "
    "listing with a description but no qualifications. When false, leave every "
    "other field empty.\n"
    "yoe_min/yoe_max: years of professional experience required, as numbers. "
    "'3+ years' is min 3 with no max; '3-5 years' is min 3 max 5; '5 years' is "
    "min 5 with no max; '0-3 years' or 'up to 3 years' is min 0 max 3. Always "
    "give yoe_min when you give yoe_max. When several are named for different "
    "skills, report the lowest that is required of the candidate overall. Leave "
    "both empty when no number of years is stated - do not infer years from a "
    "seniority word.\n"
    "degree_min: the LOWEST degree that qualifies, exactly one of none, "
    "high_school, associate, bachelors, masters, phd. 'Bachelor's or Master's' "
    "is bachelors. Leave EMPTY when the posting does not mention a degree at "
    "all; use none only when it says outright that no degree is required.\n"
    "degree_required: true when the degree is a requirement, false when it is "
    "preferred, 'a plus', or listed among nice-to-haves.\n"
    "degree_fields: fields of study named, e.g. ['Computer Science', "
    "'Electrical Engineering']. Empty when the posting names no field.\n"
    "enrollment_required: true only when the posting requires the candidate to "
    "be a currently enrolled student, or to be returning to study afterwards.\n"
    "seniority: exactly one of intern, new_grad, postdoc, entry, mid, senior, staff, "
    "principal, manager, executive. new_grad only for roles explicitly aimed at "
    "recent or upcoming graduates. Empty when the posting gives no signal.\n"
    "employment_type: exactly one of intern, full_time, part_time, contract, "
    "temporary. An internship is intern, not full_time, however many hours a "
    "week it runs. Empty when unstated.\n"
    "clearance: the security clearance required, exactly one of none, "
    "public_trust, confidential, secret, top_secret, ts_sci. Empty when the "
    "posting does not mention clearance at all; use none only when it says "
    "outright that no clearance is required.\n"
    "citizenship_required: true only when the posting requires US citizenship "
    "or permanent residency.\n"
    "sponsorship: offered when the employer states it sponsors visas for this "
    "role, not_offered when it states it will not. Empty otherwise. An "
    "application-form question asking whether the candidate needs sponsorship "
    "is NOT a statement either way. Neither is a third-party estimate of the "
    "company's sponsorship history.\n"
    "skills_required: technologies, tools, languages, frameworks, platforms and "
    "named technical methods the posting lists as required. Each entry is a "
    "NAME of at most four words, written as the posting writes it: 'Python', "
    "'Kubernetes', 'AutoCAD', 'Momentum ERP', 'finite element analysis'. Never "
    "a phrase or a sentence - write 'Plaxis', not 'Experience with Plaxis'. "
    "Exclude behavioural and interpersonal qualities (communication, teamwork, "
    "leadership, organisation, problem solving, attention to detail, work "
    "ethic, adaptability), degrees, fields of study, years of experience, "
    "clearances, certifications of eligibility to work, and job titles.\n"
    "skills_preferred: the same, for anything the posting marks as preferred, "
    "desired, bonus or nice-to-have. A skill belongs in exactly one of the two "
    "lists; when the posting does not separate them, treat them as required."
)


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
_CANDIDATES = f"""
    WITH current_row AS (
        SELECT c.url, q.content_row_id
        FROM (
            SELECT DISTINCT a.url FROM ai_queries a
            LEFT JOIN jobs j ON j.url = a.url
            WHERE j.url IS NULL OR {AI_ELIGIBLE_JOB.format(job="j")}
        ) c
        {CONTENT_LATERAL.format(url="c.url", columns="id AS content_row_id")}
    ),
    todo AS (
        SELECT cr.url, cr.content_row_id, r.content_hash AS stored_hash
        FROM current_row cr
        LEFT JOIN job_requirements r ON r.url = cr.url
        WHERE r.url IS NULL
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
    if low is None and high is None:
        return None, None
    if low is None:
        low = 0
    if high is not None and high < low:
        low, high = high, low
    # A posting asking for more than a career's worth of experience is a parse
    # slip (a year, a salary, a requisition number), and a wrong number here
    # silently reorders every "what does this market want" answer.
    if low > MAX_PLAUSIBLE_YOE:
        return None, None
    if high is not None and high > MAX_PLAUSIBLE_YOE:
        high = None
    return low, high


def _store(
    url: str, parsed: RequirementsExtract, content_hash: str, content_row_id: int | None
) -> None:
    yoe_min, yoe_max = _years(parsed)
    stated = parsed.has_requirements
    with db.pool.connection() as conn:
        conn.execute(
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
                "model": REQUIREMENTS_MODEL,
                "hash": content_hash,
                "row_id": content_row_id,
            },
        )
        # Replaced wholesale rather than merged: a re-extraction that drops a
        # skill means the posting no longer asks for it, and a left-over row
        # would keep answering the market query with a requirement that is gone.
        conn.execute("DELETE FROM job_skills WHERE url = %s", (url,))
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
            conn.cursor().executemany(
                "INSERT INTO job_skills (url, kind, skill, skill_raw) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (url, kind, skill_raw) DO NOTHING",
                rows,
            )


def _drop_unchanged_rescrapes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-stamp the pages that were scraped again but did not change.

    A url reaches the candidate list when its current content row is not the
    one its answer came from, which a re-scrape makes true whether or not the
    page actually changed - and a re-scrape that changed nothing is the common
    case. Comparing the stored hash to the new text separates the two, and the
    unchanged ones have their row id refreshed here so they do not come back
    every cycle. Nothing is paid for; the row keeps the answer it had.
    """
    changed = []
    unchanged: list[tuple[int | None, str]] = []
    for row in rows:
        text = row["input_content"][:REQUIREMENTS_INPUT_CHARS]
        row["content_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row["stored_hash"] and row["stored_hash"] == row["content_hash"]:
            unchanged.append((row["content_row_id"], row["url"]))
        else:
            changed.append(row)
    if unchanged:
        with db.pool.connection() as conn:
            conn.cursor().executemany(
                "UPDATE job_requirements SET content_row_id = %s WHERE url = %s", unchanged
            )
        logger.info(f"{len(unchanged)} page(s) re-scraped without changing; not re-extracted")
    return changed


async def handle_extract_requirements(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    rows = db.query(_CANDIDATES, {"cap": EXTRACT_REQUIREMENTS_PER_CYCLE})
    rows = _drop_unchanged_rescrapes(rows)
    if not rows:
        _set_progress(task_id, 0, 0, "nothing to extract")
        return
    schema = to_strict_json_schema(RequirementsExtract)
    specs = [
        BatchSpec(
            r["url"],
            _REQUIREMENTS_INSTRUCTIONS,
            r["input_content"][:REQUIREMENTS_INPUT_CHARS],
            "RequirementsExtract",
            schema,
        )
        for r in rows
    ]
    # The hash is of exactly the text that was sent, so a later pass can tell a
    # row extracted from today's page from one extracted from a page that has
    # since been re-scraped.
    by_url = {r["url"]: r for r in rows}
    _set_progress(task_id, 0, len(specs), "requirements batch submitted (half price)")
    results, _ = await run_batched(task_id, REQUIREMENTS_TASK, specs)
    done = 0
    for url, res in results.items():
        row = by_url.get(url)
        if row is None:
            continue
        if res.text and not res.error:
            try:
                _store(
                    url,
                    RequirementsExtract.model_validate_json(res.text),
                    row["content_hash"],
                    row["content_row_id"],
                )
            except Exception:
                # No row is written, so the next sweep picks the url up again -
                # the same idempotent-by-re-sweep contract every batched pass has.
                logger.warning(f"requirements parse failed for {url}")
        done += 1
        if done % 200 == 0:
            _set_progress(task_id, done, len(specs), "requirements extracted")
    _set_progress(task_id, done, len(specs), "requirements extracted")
