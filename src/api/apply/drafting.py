"""What a draft is written from, and the rules it is written under.

The resume, the person's own description of how they write, the instruction
text and the shape the model answers in. Read by the sweep that batches drafts
(`tasks.application`) and by the two routers that draft one live, which is why
it is here rather than inside the handler: a router reaching into a task for
these four was four of the nine exceptions on the import contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from api import db
from api.apply.writes import PURPOSE as PURPOSE
from core.answers import DEFAULT_STYLE


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
