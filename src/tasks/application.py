"""Drafts for the free-response questions on an application form.

Autofill stops at the paragraph box. The questions are public on four ATSs
(core/forms.py) and a person pastes the rest; the answer is written from a
resume they keep here, in a style they describe in their own words, about the
posting the board already holds. Drafts are batched like every other
scheduled AI step; the back-and-forth on one draft is a live call because a
person is waiting for it (routers/application.py).

Two tasks write drafts. The sweep (application_sweep, hourly per person
with a resume) reads the forms of the postings on their board and drafts
every untouched question that has no draft yet, so the answer is ready when they open
the posting and the whole day's work rides one half-price batch. The
on-demand task (application_draft) is the button: one posting, drafted or
re-drafted now.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from api import ai, budget, db, hosts
from api.apply import writes as application_writes
from api.board import visibility
from core.answers import DEFAULT_STYLE
from core.fetching import forms
from core.providers.spec import StructuredOutput
from core.routing import TaskShape
from core.store import get_content
from tasks import batch_policy
from tasks.runtime import (
    Deferred,
    consume_result,
    has_batch_work,
    load_config,
    run_batched,
    set_progress,
)

logger = logging.getLogger(__name__)

PURPOSE = application_writes.PURPOSE
IN_FLIGHT = ("pending", "running", "awaiting_batch", "waiting")

# What a stranger's answer sounds like when nobody has said otherwise. A
# person overrides the whole thing from settings; this is not merged with
# theirs, it is replaced by it.
FAULT_STYLE = (
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


# The rules the model drafts under. A change here is a roll; a change on
# the admin page (app_config application_draft_instructions) is a config
# edit and takes effect on the next draft, so wording is tuned there and
# this text is what an empty row means.
DEFAULT_INSTRUCTIONS = (
    "You draft the applicant's answer to one question on a job application form, in the "
    "first person, as the applicant. Use only facts that appear in the resume; never invent "
    "employers, dates, projects, numbers or skills. Tie what the resume shows to what the "
    "posting asks for. Match the length to the question: a factual question gets a sentence "
    "or two, a why-us or tell-us-more question gets 100 to 170 words. Plain prose, no "
    "headings, no bullet points, no em dashes. Do not flatter the company beyond what the "
    "posting itself says it does. Write only about what the resume shows. Never write a "
    "sentence saying the applicant has not done, has not worked with, does not have, or "
    'lacks something, in any phrasing: not "I have not worked with X", not "my resume '
    'does not include X", not "while I lack X", not "I have not yet". Never disclaim '
    "and never name a gap. When the question asks about something the resume does not show, "
    "answer from the closest experience it does show and say nothing about the rest. When "
    "the question asks directly and only about something the resume does not show, the "
    "answer is an empty string: the applicant writes that one themselves."
)


def instructions(style: str | None) -> str:
    rules = (db.get_config("application_draft_instructions") or "").strip() or DEFAULT_INSTRUCTIONS
    return rules + "\n\nWriting style, in the applicant's own words:\n" + (style or DEFAULT_STYLE)


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


def store_form(url: str, questions: list[forms.Question] | None, error: str | None = None) -> None:
    """One row per url: the questions as read, NULL for a host that cannot be
    read, and the error text when the read failed (questions kept as they
    were, so a transient failure does not blank a form already read)."""
    if error is not None:
        db.execute(
            "INSERT INTO application_forms (url, questions, error, fetched_at) "
            "VALUES (%s, NULL, %s, now()) ON CONFLICT (url) DO UPDATE "
            "SET error = EXCLUDED.error, fetched_at = now()",
            (url, error[:300]),
        )
        return
    payload = None if questions is None else [q.as_dict() for q in questions]
    db.execute(
        "INSERT INTO application_forms (url, questions, error, fetched_at) "
        "VALUES (%s, %s, NULL, now()) ON CONFLICT (url) DO UPDATE "
        "SET questions = EXCLUDED.questions, error = NULL, fetched_at = now()",
        (url, db.jsonb(payload) if payload is not None else None),
    )


def read_form(url: str) -> list[dict[str, Any]] | None:
    """Read the form under the host's budget and store it. Returns the
    questions, or None for a host that cannot be read. Raises Deferred when
    the host's slot is closed or the host refused, and re-raises a failed
    read after recording it."""
    if not forms.supported(url):
        store_form(url, None)
        return None
    host = forms.budget_host(url)
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
        store_form(url, None, error=str(exc))
        raise
    hosts.succeeded(host)
    store_form(url, questions or [])
    return [q.as_dict() for q in questions or []]


def sync_form_questions(url: str, *, refresh: bool = False) -> list[dict[str, Any]] | None:
    """The form's questions for this url, read once and cached. None when the
    host cannot be read. Raises Deferred when the host's slot is closed."""
    if not refresh:
        row = db.query_one("SELECT questions, error FROM application_forms WHERE url = %s", (url,))
        if row and row["error"] is None:
            return row["questions"]
    return read_form(url)


def ensure_answer_rows(
    user_id: int, job_id: int, questions: list[dict[str, Any]], *, task_id: int | None = None
) -> None:
    with db.transaction():
        # Admission locks job -> task -> answers. Inserts also acquire a job
        # foreign-key lock, so take that first rather than after answer locks.
        db.query_one("SELECT id FROM jobs WHERE id = %s FOR KEY SHARE", (job_id,))
        task = (
            db.query_one("SELECT payload FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
            if task_id
            else None
        )
        requests = (task["payload"].get("draft_requests") or {}) if task else {}
        db.query(
            "SELECT id FROM application_answers WHERE user_id = %s AND job_id = %s ORDER BY id FOR UPDATE",
            (user_id, job_id),
        )
        for q in sorted(questions, key=lambda value: value["key"]):
            owned_revision = requests.get(f"{job_id}|{q['key']}", {}).get("revision")
            db.execute(
                """
                INSERT INTO application_answers (user_id, job_id, key, question, source, required)
                VALUES (%s, %s, %s, %s, 'form', %s)
                ON CONFLICT (user_id, job_id, key) DO UPDATE
                    SET question = EXCLUDED.question, required = EXCLUDED.required,
                        draft_revision = CASE
                            WHEN application_answers.question IS DISTINCT FROM EXCLUDED.question
                             AND application_answers.draft_revision IS DISTINCT FROM %s
                            THEN application_answers.draft_revision + 1
                            ELSE application_answers.draft_revision END
                """,
                (user_id, job_id, q["key"], q["label"], bool(q.get("required")), owned_revision),
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


def auto_draft(user_id: int) -> bool:
    """Whether the sweep drafts for this person. On unless they turned it off
    (prefs.auto_draft = false); the resume is what opts a person in."""
    row = db.query_one(
        "SELECT prefs->>'auto_draft' AS v FROM user_settings WHERE user_id = %s", (user_id,)
    )
    return (row or {}).get("v") != "false"


def _set_draft_progress(task_id: int, label: str, minimum_total: int) -> None:
    done, total = application_writes.progress_counts(task_id, minimum_total)
    set_progress(task_id, done, total, label + application_writes.outcome_note(task_id))


async def _batch_drafts(
    task_id: int, user_id: int, specs: list, kind: str, *, resumed: bool
) -> int:
    results, chosen = await run_batched(task_id, APPLICATION_TASK, specs, charged_to_user=True)
    done = 0
    for res in results:
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            parsed = None
            if res.error or not res.text:
                logger.warning("application draft %s: %s", res.custom_id, res.error or "empty")
            else:
                try:
                    parsed = Draft.model_validate_json(res.text)
                except ValueError:
                    logger.warning("application draft %s: unparsable", res.custom_id)
            request = res.request.context if res.request is not None else None
            if request is None:
                # Earlier submissions may have an original task reservation
                # without a full request snapshot. Never invent a generation.
                task = db.query_one(
                    "SELECT payload->'draft_requests' AS requests FROM tasks WHERE id = %s",
                    (task_id,),
                )
                request = ((task or {}).get("requests") or {}).get(res.custom_id)
            receipt.outcome = application_writes.apply_result(
                user_id,
                request,
                parsed.answer if parsed is not None else None,
                ai.batch_usage(res.usage),
                "owner",
                res.model if resumed else chosen.model,
                kind,
                batched=True,
            )
            done += int(receipt.outcome == "written")
    return done


def _scheduled_config(task_id: int, user_id: int) -> ai.AIConfig:
    _, cfg = load_config(user_id)
    if cfg.key_source == "owner":
        # Application batches resolve APPLICATION_TASK independently of the
        # person's interactive model; only credential transport gates this run.
        batch_policy.require_transport(task_id, cfg)
    return cfg


async def draft_rows(
    task_id: int,
    user_id: int,
    rows: list[dict[str, Any]],
    resume_id: int | None = None,
    kind: str = "draft",
    *,
    scheduled: bool = False,
) -> int:
    """Write a draft for each row (job_id, url, company, title, key,
    question), for one person, from their resume and style: one half-price
    batch on the fleet's key, or live one at a time on their own. Returns
    how many drafts were written. Scheduled callers require batch transport on the shared key.
    Safe to run again from the top: a resumed
    task collects its original results and applies only its own generations."""
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    if has_batch_work(task_id):
        return await _batch_drafts(task_id, user_id, [], kind, resumed=True)
    cfg = _scheduled_config(task_id, user_id) if scheduled else None
    resume = resume_text(user_id, resume_id)
    if not resume:
        raise RuntimeError("no resume on file; add one under settings first")
    text = instructions(writing_style(user_id))
    schema = to_strict_json_schema(Draft)
    postings: dict[str, str] = {}
    specs = []
    reserved = application_writes.reserve_task(task_id, user_id, rows)
    for r in rows:
        if f"{r['job_id']}|{r['key']}" not in reserved:
            continue
        if r["url"] not in postings:
            postings[r["url"]] = get_content(r["url"]) or ""
        specs.append(
            BatchSpec(
                # job first, then the key: a key never carries a bar.
                f"{r['job_id']}|{r['key']}",
                text,
                question_input(
                    reserved[f"{r['job_id']}|{r['key']}"]["question"],
                    r["company"],
                    r["title"],
                    postings[r["url"]],
                    resume,
                ),
                "Draft",
                schema,
                context=reserved[f"{r['job_id']}|{r['key']}"],
            )
        )
    if not specs:
        return 0
    if cfg is None:
        _, cfg = load_config(user_id)
    total = len(specs)
    done = 0
    if cfg.key_source == "owner" and cfg.provider == "openai":
        # The fleet's sanctioned writer, overridable from the task screen like
        # any other step; the tokens are the person's, booked below, so the
        # standard caller is told not to book them against the fleet as well.
        _set_draft_progress(task_id, f"{total} draft(s) submitted (half price)", total)
        done = await _batch_drafts(task_id, user_id, specs, kind, resumed=False)
    else:
        # A person's own key has no batch endpoint we can bill to them; one
        # live call per question, the way their filters run.
        for spec in specs:
            with budget.record_parse_failures(user_id, cfg.key_source, PURPOSE, cfg.model):
                parsed, usage = await ai.parse(cfg, spec.instructions, spec.input, Draft)
            done += application_writes.record_result(
                task_id,
                user_id,
                spec.custom_id,
                parsed.answer if parsed is not None else None,
                usage,
                cfg.key_source,
                cfg.model,
                kind,
                batched=False,
            )
            _set_draft_progress(task_id, "drafting", total)
    return done


async def handle_application_draft(task_id: int, payload: dict[str, Any]) -> None:
    """The button. Payload: user_id, job_id, optional resume_id, keys (only
    these questions), refresh (re-read the form). Drafts every question of
    the posting, drafted before or not."""
    user_id, job_id = payload["user_id"], payload["job_id"]
    if has_batch_work(task_id):
        await draft_rows(task_id, user_id, [], payload.get("resume_id"))
        _set_draft_progress(task_id, "drafts collected", 0)
        return
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
            task_id=task_id,
        )
    rows = db.query(
        "SELECT a.job_id, a.key, a.question, j.url, j.company, j.title "
        "FROM application_answers a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.user_id = %s AND a.job_id = %s"
        + (" AND a.key = ANY(%s)" if keys else "")
        + " ORDER BY a.id",
        (user_id, job_id, keys) if keys else (user_id, job_id),
    )
    if not rows:
        set_progress(task_id, 0, 0, "no questions to answer")
        return
    await draft_rows(task_id, user_id, rows, payload.get("resume_id"))
    _set_draft_progress(task_id, "drafts written", len(rows))


async def handle_application_sweep(task_id: int, payload: dict[str, Any]) -> None:
    """Ahead of need, for one person: read the forms of the postings on
    their board that have not been read (newest first, a bounded number per
    cycle, skipping a host whose slot is closed rather than waiting on it),
    open a row for every paragraph question, and draft every row without a
    draft in one batch. Nothing is re-drafted: the button does that."""
    user_id = payload["user_id"]
    # Collect before reading another cap of forms. On 2026-09-06 the first
    # sweep read 113 forms before submission and 114 more on resume, spending
    # two cycles of reads on one batch and reporting 60 of 158 done.
    if has_batch_work(task_id):
        await draft_rows(task_id, user_id, [], kind="sweep", scheduled=True)
        _set_draft_progress(task_id, "drafts collected", 0)
        return
    # One sweep per person at a time. A parked sweep frees its worker, so
    # the next hourly one was claimed while a manual full-board sweep sat on
    # its batch (2026-09-07 00:00Z): both selected the same undrafted rows,
    # and only the order the batches landed in kept them from drafting the
    # same answers twice. The later one waits for the next cycle instead.
    # Bounded to a day: the batch poll resumes a parked task once its batch
    # is past the provider's completion window (24 h) whatever state it is
    # in, so an earlier sweep older than that is a stuck task, not a live
    # one, and must not turn one stuck sweep into no sweeps forever.
    other = db.query_one(
        """
        SELECT id FROM tasks
        WHERE kind = 'application_sweep' AND id <> %(tid)s
          AND status IN ('pending', 'running', 'awaiting_batch', 'waiting')
          AND (payload->>'user_id')::bigint = %(uid)s
          AND id < %(tid)s
          AND created_at > now() - interval '1 day'
        LIMIT 1
        """,
        {"tid": task_id, "uid": user_id},
    )
    if other:
        set_progress(task_id, 0, 0, f"sweep {other['id']} for this person is still in flight")
        return
    if not auto_draft(user_id):
        set_progress(task_id, 0, 0, "automatic drafts are off for this person")
        return
    if not resume_text(user_id, None):
        set_progress(task_id, 0, 0, "no resume on file")
        return
    # Refuse unsupported shared credentials before spending reads. Revalidate
    # in draft_rows after that phase, before reserving answer generations.
    _scheduled_config(task_id, user_id)
    reads_cap = int(db.get_config("application_form_reads_per_cycle"))
    drafts_cap = int(db.get_config("application_drafts_per_cycle"))

    # "On their board" is the board's own membership predicate
    # (api.board.visibility.FAST), not a fresh spelling of it.
    unread = db.query(
        visibility.FAST.format(
            columns="j.url",
            extra=(
                "AND j.active AND NOT EXISTS "
                "(SELECT 1 FROM application_forms f WHERE f.url = j.url) "
                "ORDER BY j.created_at DESC LIMIT %(n)s"
            ),
        ),
        {"uid": user_id, "n": reads_cap},
    )
    read = skipped = failed = 0
    for r in unread:
        try:
            read_form(r["url"])
            read += 1
        except Deferred:
            skipped += 1
        except Exception:
            failed += 1
    # Kept on the final label too: the form phase is the part that can
    # quietly stall (every host busy, every read failing), and its count
    # is the one thing a person reads off the task afterwards.
    note = f" (forms: {read} read, {skipped} host busy, {failed} failed)" if unread else ""
    if unread:
        set_progress(task_id, 0, 0, note.strip(" ()"))

    # A row for every paragraph question on a board posting whose form is
    # read and that this person has no rows for yet.
    for r in db.query(
        visibility.FAST.format(
            columns=(
                "j.id AS job_id, (SELECT f.questions FROM application_forms f "
                "WHERE f.url = j.url) AS questions"
            ),
            extra=(
                "AND j.active "
                "AND EXISTS (SELECT 1 FROM application_forms f "
                "            WHERE f.url = j.url AND f.questions IS NOT NULL) "
                "AND NOT EXISTS (SELECT 1 FROM application_answers a "
                "                WHERE a.user_id = %(uid)s AND a.job_id = j.id)"
            ),
        ),
        {"uid": user_id},
    ):
        ensure_answer_rows(
            user_id, r["job_id"], [q for q in r["questions"] if q.get("kind") == "long"]
        )

    on_board = [
        r["id"]
        for r in db.query(
            visibility.FAST.format(
                columns="j.id",
                extra=(
                    "AND j.active AND EXISTS (SELECT 1 FROM application_answers a "
                    "WHERE a.user_id = %(uid)s AND a.job_id = j.id AND a.draft IS NULL)"
                ),
            ),
            {"uid": user_id},
        )
    ]
    rows = db.query(
        """
        SELECT a.job_id, a.key, a.question, j.url, j.company, j.title
        FROM application_answers a JOIN jobs j ON j.id = a.job_id
        WHERE a.user_id = %s AND a.job_id = ANY(%s) AND a.draft IS NULL
        ORDER BY j.created_at DESC, a.id LIMIT %s
        """,
        (user_id, on_board, drafts_cap),
    )
    if not rows:
        set_progress(task_id, 0, 0, "nothing new to draft" + note)
        return
    await draft_rows(task_id, user_id, rows, kind="sweep", scheduled=True)
    _set_draft_progress(task_id, "drafts written ahead of need" + note, len(rows))
