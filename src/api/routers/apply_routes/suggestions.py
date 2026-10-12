"""Model suggestions for unresolved application form fields."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import ai, apply, budget, db
from api.ai import access as ai_access
from api.apply import drafting as drafts
from api.apply import fill_answers, posting_context
from api.auth import AuthedUser, require_user
from api.board.person_state import touchable_job_ids
from api.problem import AI_REFUSALS, refuse
from api.routers.apply_routes.events import record_apply_event
from api.routers.apply_routes.models import Suggested
from core.store import get_content

router = APIRouter()


class SuggestField(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    label: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="text", max_length=20)
    options: apply.ApplicationOptions = Field(default_factory=list)
    # The profile's answer when the options did not recognisably hold it.
    hint: str | None = Field(default=None, max_length=200)


class SuggestBody(BaseModel):
    fields: list[SuggestField] = Field(min_length=1, max_length=100)
    job_id: int | None = None
    # The fill this belongs to; the model's answers go on its ledger row.
    fill_id: int | None = None
    review_only: bool = False
    revision: int | None = Field(default=None, ge=0)


class SuggestedAnswer(BaseModel):
    key: str
    answer: str


class Suggestions(BaseModel):
    answers: list[SuggestedAnswer]


DEFAULT_SUGGEST = (
    "You fill the fields of a job application form that the person's profile "
    "did not fill by rule. Answer every field from the profile and resume; do "
    "not invent facts. For a field with options, the answer is exactly one "
    "of the options as written; when a hint is given, it is the person's own "
    "answer and you choose the option that means it. A text field takes a "
    "short phrase, a number field a number, and a long field a short paragraph "
    "in the person's own voice drawn from the resume. The resume and profile are "
    "the person's whole record: a question about their own history with this "
    "employer or its people (worked here before, applied before, referred by or "
    "related to an employee, a current or former contractor) is answered No when "
    "neither shows it. A fact only they could know (a postal code, a date) is "
    "left as an empty string rather than guessed."
)


# The answers are split on this separator before each item is matched, so it
# is part of the parsing contract and is appended whatever the configured
# wording says.
MULTI_CHOICE = (
    " A multiselect field's answer names every chosen option, each exactly as "
    "written, joined by ' | '."
)


def never_filled(fields: list[SuggestField]) -> list[str]:
    """The keys of the fields the admin list keeps from the model."""
    raw = db.get_config("application_ai_never_fills") or ""
    words = [w.strip().lower() for w in re.split(r"[\n,]", raw) if w.strip()]
    if not words:
        return []
    return [
        f.key
        for f in fields
        if any(re.search(rf"\b{re.escape(w)}\b", f"{f.label} {f.key}".lower()) for w in words)
    ]


@router.post("/user/apply/suggest", responses=AI_REFUSALS)
async def suggest(body: SuggestBody, user: AuthedUser = Depends(require_user)) -> Suggested:
    """Everything the ladder left blank, in one live call on the person's
    own model settings. The extension fills the answers; the person still
    sees them in the form before submitting, and an answer left in place
    goes into the bank on submit."""
    token = None
    saved = {}
    if body.review_only and (
        body.fill_id is None or body.revision is None or len(body.fields) != 1
    ):
        raise refuse(
            422, "FILL_REQUIRED", "Answer review requires one field, its revision and its fill."
        )
    # Touchable, the rule resolve_form matches a fill's posting by: the
    # posting text goes into the prompt, and another person's private upload
    # is not this person's to read.
    if body.job_id and not touchable_job_ids(user.id, [body.job_id]):
        raise refuse(404, "NOT_FOUND", "unknown job")
    if body.fill_id is not None:
        token, saved = fill_answers.reserve(
            user.id,
            body.fill_id,
            [f.key for f in body.fields],
            body.job_id,
            expected_revision=body.revision if body.review_only else None,
        )
    skipped = never_filled(body.fields)
    fields = [f for f in body.fields if f.key not in skipped]
    if not fields:
        _finish_suggestions(user.id, body, token, {}, {}, skipped)
        record_apply_event(
            "apply_ai_suggested", user, asked=0, answered=0, skipped=len(skipped), model=None
        )
        return Suggested(answers={}, skipped=skipped, model=None)
    profile = apply.load_profile(user.id)
    resume = drafts.resume_text(user.id, profile.default_resume_id) or ""
    cfg = ai_access.require_config(user)
    job = (
        db.query_one("SELECT company, title, url FROM jobs WHERE id = %s", (body.job_id,))
        if body.job_id
        else None
    )
    parts = []
    if job:
        parts.append(f"Job: {job['title']} at {job['company']}")
    if any(f.kind in {"text", "long"} and not f.options for f in fields):
        posting = get_content(job["url"]) if job else None
        if not posting and body.fill_id is not None:
            posting = await posting_context.external_posting(user.id, body.fill_id)
        parts.append(f"Posting:\n{(posting or '')[:6000] or '(no posting text captured)'}")
    parts.append("Fields:\n" + json.dumps([f.model_dump() for f in fields], indent=1))
    if body.review_only:
        parts.append(
            "Revise the requested answers using the person's saved feedback. "
            "Keep claims grounded in their profile and resume.\n"
            + json.dumps(
                {
                    key: {
                        "previous_answer": f.get("review_value", f.get("value")),
                        "feedback": f.get("feedback", ""),
                    }
                    for key, f in saved.items()
                }
            )
        )
    if profile.notes:
        parts.append("The person's standing answers, in their own words:\n" + profile.notes)
    parts.append("Profile:\n" + profile.model_dump_json(exclude={"default_resume_id", "notes"}))
    if resume:
        parts.append("Resume:\n" + resume)
    rules = (db.get_config("application_suggest_instructions") or "").strip() or DEFAULT_SUGGEST
    rules = drafts.with_answer_guidance(rules, drafts.writing_style(user.id)) + MULTI_CHOICE
    with budget.record_parse_failures(user.id, cfg.key_source, drafts.PURPOSE, cfg.model):
        parsed, usage = await ai.parse(cfg, rules, "\n\n".join(parts), Suggestions)
    budget.record_tokens(
        user.id,
        cfg.key_source,
        drafts.PURPOSE,
        cfg.model,
        usage,
    )
    if parsed is None:
        raise refuse(502, "NO_ANSWER", "the model returned no usable answer; try again")
    by_key = {f.key: f for f in fields}
    answers = {}
    raw = {}
    for a in parsed.answers:
        field = by_key.get(a.key)
        text = a.answer.strip()
        if not field or not text:
            continue
        raw[a.key] = text
        if field.options:
            pick = apply.pick_options if field.kind == "multiselect" else apply.pick_option
            text = pick(text, field.options) or ""
        if text:
            answers[a.key] = text
    _finish_suggestions(user.id, body, token, answers, raw, skipped)
    record_apply_event(
        "apply_ai_suggested",
        user,
        asked=len(fields),
        answered=len(answers),
        skipped=len(skipped),
        model=cfg.model,
    )
    return Suggested(answers=answers, skipped=skipped, model=cfg.model)


def _finish_suggestions(
    user_id: int, body: SuggestBody, token: str | None, answers: dict, raw: dict, skipped: list[str]
) -> None:
    if body.fill_id is None or token is None:
        return
    if not fill_answers.complete(
        user_id,
        body.fill_id,
        token,
        [f.key for f in body.fields],
        answers,
        raw,
        skipped,
        review_only=body.review_only,
    ):
        raise refuse(409, "ANSWER_CHANGED", "A newer edit or submission superseded this answer.")
