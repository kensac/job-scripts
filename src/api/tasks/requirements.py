"""Structured requirements extraction: what a posting actually asks for."""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from pydantic import BaseModel

from api import db
from api.tasks.runtime import (
    _batch_event_hook,
    _pending_batch_ids,
    _set_progress,
    submit_or_collect,
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
from core.routing import TaskShape, resolve
from core.store import CONTENT_LATERAL

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
REQUIREMENTS_MODEL = "gpt-5-mini"


# "minimal" is not a cost compromise, it is the better answer: low-effort
# reasoning was 78% of the output bill (802 output tokens per posting against
# 194) and bought nothing this task needs, since every field is copied off the
# page rather than deduced.
REQUIREMENTS_REASONING_EFFORT = "minimal"


# Measured p100 over the pilot was 378 tokens; the JSON's whole variable part is
# the two skill arrays, so the budget is set to carry roughly three times the
# widest skill list seen. An unfinished response is unparseable JSON, and an
# unparseable line leaves the url unextracted for the next sweep to re-pay
# forever, so the headroom is worth more than the tokens - a cap is a ceiling,
# not a charge.
REQUIREMENTS_MAX_OUTPUT_TOKENS = 1000


# The same declaration every other batched extraction makes. The model is the
# caller's judgment - the note above is why mini and not nano - and the router
# checks it can do what this task needs rather than choosing it for cost.
# est_prompt_tokens is the fitted figure from the pilot below, not the
# conservative chunking estimate: it ranks candidates and never becomes a bill.
REQUIREMENTS_TASK = TaskShape(
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=REQUIREMENTS_MAX_OUTPUT_TOKENS,
    est_prompt_tokens=2270,
    effort=REQUIREMENTS_REASONING_EFFORT,
    candidates=(REQUIREMENTS_MODEL,),
)


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


# Only postings whose stored page text has not already been extracted. Keyed on
# the url rather than on a job id: 5,511 of the 20,680 urls with usable content
# have no jobs row at all, and those postings are closed and unscrapable, which
# makes them the part of the corpus that can never be rebuilt.
#
# The already-extracted urls are filtered out BEFORE the lateral runs, not
# after. input_content is TOASTed, so a plan that joins first and filters
# second detoasts megabytes of page text for urls it is about to discard;
# measured against prod, this shape touches only the rows it returns.
_CANDIDATES = f"""
    SELECT c.url, q.input_content
    FROM (
        SELECT DISTINCT a.url FROM ai_queries a
        WHERE NOT EXISTS (SELECT 1 FROM job_requirements r WHERE r.url = a.url)
    ) c
    {CONTENT_LATERAL.format(url="c.url")}
    LIMIT %(cap)s
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


def _store(url: str, parsed: RequirementsExtract, content_hash: str) -> None:
    yoe_min, yoe_max = _years(parsed)
    stated = parsed.has_requirements
    with db.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO job_requirements (
                url, has_requirements, yoe_min, yoe_max, degree_min, degree_required,
                degree_fields, enrollment_required, seniority, employment_type,
                clearance, citizenship_required, sponsorship, model, content_hash)
            VALUES (%(url)s, %(has)s, %(ymin)s, %(ymax)s, %(deg)s, %(degreq)s,
                    %(fields)s, %(enrol)s, %(sen)s, %(emp)s, %(clr)s, %(cit)s,
                    %(spon)s, %(model)s, %(hash)s)
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


async def handle_extract_requirements(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    rows = db.query(_CANDIDATES, {"cap": EXTRACT_REQUIREMENTS_PER_CYCLE})
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
    hashes = {
        r["url"]: hashlib.sha256(
            r["input_content"][:REQUIREMENTS_INPUT_CHARS].encode("utf-8")
        ).hexdigest()
        for r in rows
    }
    _set_progress(task_id, 0, len(specs), "requirements batch submitted (half price)")
    chosen = resolve(REQUIREMENTS_TASK)
    logger.info(f"Task {task_id}: requirements extraction on {chosen.model} - {chosen.reason}")
    hook = _batch_event_hook(task_id, "requirements", chosen.model)
    existing = _pending_batch_ids(task_id)
    if existing:
        from core.batch import collect_batches

        logger.info(f"Task {task_id}: reattaching to {len(existing)} in-flight batch(es)")
        results = await collect_batches(existing, hook)
    else:
        results = await submit_or_collect(
            task_id,
            specs,
            chosen.model,
            REQUIREMENTS_TASK.resolved_effort() or REQUIREMENTS_REASONING_EFFORT,
            REQUIREMENTS_MAX_OUTPUT_TOKENS,
            hook,
        )
    done = 0
    for url, res in results.items():
        content_hash = hashes.get(url)
        if content_hash is None:
            continue
        if res.text and not res.error:
            try:
                _store(url, RequirementsExtract.model_validate_json(res.text), content_hash)
            except Exception:
                # No row is written, so the next sweep picks the url up again -
                # the same idempotent-by-re-sweep contract every batched pass has.
                logger.warning(f"requirements parse failed for {url}")
        done += 1
        if done % 200 == 0:
            _set_progress(task_id, done, len(specs), "requirements extracted")
    _set_progress(task_id, done, len(specs), "requirements extracted")
