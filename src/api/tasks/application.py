"""Drafts for the free-response questions on an application form.

Autofill stops at the paragraph box. The questions are public on four ATSs
(core/forms.py) and a person pastes the rest; the answer is written from a
resume they keep here, in a style they describe in their own words, about the
posting the board already holds. Drafts are batched like every other
scheduled AI step; the back-and-forth on one draft is a live call because a
person is waiting for it (routers/application.py).
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from pydantic import BaseModel

from api import ai, budget, db, hosts
from api.tasks.runtime import (
    Deferred,
    load_config,
    run_batched,
    set_progress,
)
from core import forms
from core.providers.spec import StructuredOutput
from core.routing import TaskShape
from core.store import get_content

logger = logging.getLogger("jobtracker_worker")

PURPOSE = "application"

# What a stranger's answer sounds like when nobody has said otherwise. A
# person overrides the whole thing from settings; this is not merged with
# theirs, it is replaced by it.
DEFAULT_STYLE = (
    "Concise but not abrupt: the shortest version that still has enough context to feel "
    "thoughtful. Natural and conversational, like something a person would actually type, "
    "not polished corporate language. Professional without being formal. Simple wording over "
    "jargon or buzzwords. Specific rather than generic: name the actual project, situation or "
    "reason instead of filler. Confident but understated: show competence through what was "
    "done and how, never by declaring it. Low fluff: no excessive gratitude, pleasantries or "
    "repetition."
)

APPLICATION_TASK = TaskShape(
    purpose=PURPOSE,
    label="Application answers",
    per_cycle=0,
    notes=(
        "Prose a recruiter reads, written from a resume the model must not embroider. "
        "Measured 2026-09-06 over four live questions: gpt-5.6-luna without reasoning picked "
        "the resume facts that fit the role and stayed inside them; gpt-5-nano padded with "
        "generic openers and loosened one claim. Both cost under a tenth of a cent an answer, "
        "so the better writer is the choice."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=1200,
    est_prompt_tokens=3500,
    effort_preference=("none", "minimal", "low"),
    candidates=("gpt-5.6-luna",),
)


class Draft(BaseModel):
    answer: str


def instructions(style: str | None) -> str:
    return (
        "You draft the applicant's answer to one question on a job application form, in the "
        "first person, as the applicant. Use only facts that appear in the resume; never invent "
        "employers, dates, projects, numbers or skills. Tie what the resume shows to what the "
        "posting asks for. Match the length to the question: a factual question gets a sentence "
        "or two, a why-us or tell-us-more question gets 100 to 170 words. Plain prose, no "
        "headings, no bullet points, no em dashes. Do not flatter the company beyond what the "
        "posting itself says it does. When the resume does not cover what the question asks, "
        "say so briefly in the answer rather than making something up, so the applicant can fill "
        "it in.\n\nWriting style, in the applicant's own words:\n" + (style or DEFAULT_STYLE)
    )


def question_input(
    question: str,
    company: str,
    title: str,
    posting: str,
    resume: str,
    *,
    draft: str | None = None,
    turns: list[dict[str, Any]] | None = None,
    instruction: str | None = None,
) -> str:
    parts = [
        f"Question on the form: {question}",
        f"Company: {company}\nRole: {title}",
        f"Posting:\n{posting[:6000] or '(no posting text captured)'}",
        f"Resume:\n{resume[:12000]}",
    ]
    if draft:
        parts.append(f"Current draft:\n{draft}")
    for t in turns or []:
        if t.get("role") == "user":
            parts.append(f"Earlier request from the applicant: {t.get('text', '')}")
    if instruction:
        parts.append(f"Revise the current draft as the applicant asks: {instruction}")
    return "\n\n".join(parts)


def sync_form_questions(url: str, *, refresh: bool = False) -> list[dict[str, Any]] | None:
    """The form's questions for this url, read once and cached. None when the
    host cannot be read. Raises Deferred when the host's slot is closed."""
    if not refresh:
        row = db.query_one("SELECT questions, error FROM application_forms WHERE url = %s", (url,))
        if row and row["error"] is None:
            return row["questions"]
    if not forms.supported(url):
        db.execute(
            "INSERT INTO application_forms (url, questions, error, fetched_at) "
            "VALUES (%s, NULL, NULL, now()) ON CONFLICT (url) DO UPDATE "
            "SET questions = NULL, error = NULL, fetched_at = now()",
            (url,),
        )
        return None
    host = forms.host_of(url)
    opens = hosts.take(host)
    if opens:
        raise Deferred(opens)
    try:
        questions = forms.fetch(url)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            hosts.refused(host)
            raise Deferred(hosts.soon()) from exc
        db.execute(
            "INSERT INTO application_forms (url, questions, error, fetched_at) "
            "VALUES (%s, NULL, %s, now()) ON CONFLICT (url) DO UPDATE "
            "SET error = EXCLUDED.error, fetched_at = now()",
            (url, str(exc)[:300]),
        )
        raise
    hosts.succeeded(host)
    payload = [q.as_dict() for q in questions or []]
    db.execute(
        "INSERT INTO application_forms (url, questions, error, fetched_at) "
        "VALUES (%s, %s, NULL, now()) ON CONFLICT (url) DO UPDATE "
        "SET questions = EXCLUDED.questions, error = NULL, fetched_at = now()",
        (url, db.jsonb(payload)),
    )
    return payload


def ensure_answer_rows(user_id: int, job_id: int, questions: list[dict[str, Any]]) -> None:
    for q in questions:
        db.execute(
            """
            INSERT INTO application_answers (user_id, job_id, key, question, source, required)
            VALUES (%s, %s, %s, %s, 'form', %s)
            ON CONFLICT (user_id, job_id, key) DO UPDATE
                SET question = EXCLUDED.question, required = EXCLUDED.required
            """,
            (user_id, job_id, q["key"], q["label"], bool(q.get("required"))),
        )


def resume_text(user_id: int, resume_id: int | None) -> str | None:
    if resume_id is not None:
        row = db.query_one(
            "SELECT text FROM user_resumes WHERE user_id = %s AND id = %s", (user_id, resume_id)
        )
    else:
        row = db.query_one(
            "SELECT text FROM user_resumes WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        )
    return row["text"] if row else None


def writing_style(user_id: int) -> str | None:
    row = db.query_one("SELECT writing_style FROM user_settings WHERE user_id = %s", (user_id,))
    return (row or {}).get("writing_style") or None


def store_draft(user_id: int, job_id: int, key: str, answer: str, model: str) -> None:
    turn = {
        "role": "assistant",
        "text": answer,
        "at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    db.execute(
        """
        UPDATE application_answers
           SET draft = %s, model = %s, turns = turns || %s::jsonb, updated_at = now()
         WHERE user_id = %s AND job_id = %s AND key = %s
        """,
        (answer, model, json.dumps([turn]), user_id, job_id, key),
    )


def _usage_of(res: Any) -> dict[str, int]:
    u = res.usage or {}
    return {
        "prompt_tokens": u.get("input_tokens", 0),
        "completion_tokens": u.get("output_tokens", 0),
        "total_tokens": u.get("total_tokens", 0),
    }


async def handle_application_draft(task_id: int, payload: dict[str, Any]) -> None:
    """Payload: user_id, job_id, optional resume_id, keys (only these
    questions), refresh (re-read the form). Safe to run again from the top:
    a resumed task collects the batch it parked on and overwrites drafts."""
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    user_id, job_id = payload["user_id"], payload["job_id"]
    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise LookupError("unknown job")
    questions = sync_form_questions(job["url"], refresh=bool(payload.get("refresh")))
    keys = payload.get("keys")
    if questions:
        # The paragraph boxes by default. A one-line box (a URL, a start
        # date) is drafted only when asked for by key: a live Greenhouse form
        # carried seven of them beside two real questions, and a draft for
        # "LinkedIn Profile" is noise the person has to delete.
        ensure_answer_rows(
            user_id,
            job_id,
            [q for q in questions if q.get("kind") == "long" or q["key"] in (keys or [])],
        )
    rows = db.query(
        "SELECT key, question FROM application_answers WHERE user_id = %s AND job_id = %s"
        + (" AND key = ANY(%s)" if keys else "")
        + " ORDER BY id",
        (user_id, job_id, keys) if keys else (user_id, job_id),
    )
    if not rows:
        set_progress(task_id, 0, 0, "no questions to answer")
        return
    resume = resume_text(user_id, payload.get("resume_id"))
    if not resume:
        raise RuntimeError("no resume on file; add one under settings first")
    text = instructions(writing_style(user_id))
    posting = get_content(job["url"]) or ""
    schema = to_strict_json_schema(Draft)
    specs = [
        BatchSpec(
            r["key"],
            text,
            question_input(r["question"], job["company"], job["title"], posting, resume),
            "Draft",
            schema,
        )
        for r in rows
    ]
    _, cfg = load_config(user_id)
    total = len(specs)
    done = 0
    if cfg.key_source == "owner" and cfg.provider == "openai":
        # The fleet's sanctioned writer, overridable from the task screen like
        # any other step; the tokens are the person's, booked below, so the
        # standard caller is told not to book them against the fleet as well.
        set_progress(task_id, 0, total, f"{total} draft(s) submitted (half price)")
        results, chosen = await run_batched(task_id, APPLICATION_TASK, specs, charged_to_user=True)
        for key, res in results.items():
            usage = _usage_of(res)
            if usage["total_tokens"]:
                budget.record_usage(
                    user_id,
                    cfg.key_source,
                    PURPOSE,
                    chosen.model,
                    usage["prompt_tokens"],
                    usage["completion_tokens"],
                    usage["total_tokens"],
                )
            if res.error or not res.text:
                logger.warning(f"application draft {key} for job {job_id}: {res.error or 'empty'}")
                continue
            try:
                answer = Draft.model_validate_json(res.text).answer
            except Exception:
                logger.warning(f"application draft {key} for job {job_id}: unparsable")
                continue
            store_draft(user_id, job_id, key, answer, chosen.model)
            done += 1
    else:
        # A person's own key has no batch endpoint we can bill to them; one
        # live call per question, the way their filters run.
        for spec in specs:
            parsed, usage = await ai.parse(cfg, spec.instructions, spec.input, Draft)
            budget.record_usage(
                user_id,
                cfg.key_source,
                PURPOSE,
                cfg.model,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )
            if parsed:
                store_draft(user_id, job_id, spec.custom_id, parsed.answer, cfg.model)
                done += 1
            set_progress(task_id, done, total, "drafting")
    set_progress(task_id, done, total, "drafts written")
