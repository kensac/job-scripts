"""A person's apply profile, answer bank, and prefill context."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from api import apply, db, user_settings
from api.auth import AuthedUser, require_user
from api.mail import applications
from api.models import Ok
from api.problem import refuse
from api.routers.apply_routes.models import (
    AnswerList,
    ApplyContext,
    ContextDraft,
    MatchedPosting,
    RememberedAnswer,
)
from core.fetching.forms import posting_urls

router = APIRouter()
_BANK_COLS = "id, label, kind, value, times_used, last_used_at, updated_at"


@router.get("/user/profile")
def get_profile(user: AuthedUser = Depends(require_user)) -> apply.Profile:
    return apply.load_profile(user.id)


class ProfilePut(apply.Profile):
    """Replace the whole profile; omitted fields return to their defaults."""

    # Stored profiles stay tolerant of older keys. Writes cannot silently drop
    # misspelled fields while replacing the person's previously saved facts.
    model_config = ConfigDict(extra="forbid")


@router.put("/user/profile")
def put_profile(body: ProfilePut, user: AuthedUser = Depends(require_user)) -> apply.Profile:
    """The whole profile, replaced. A resume id that is not the person's is
    dropped rather than refused, so a deleted resume does not wedge the
    profile."""
    if body.default_resume_id is not None and not db.query_one(
        "SELECT 1 FROM user_resumes WHERE id = %s AND user_id = %s",
        (body.default_resume_id, user.id),
    ):
        body.default_resume_id = None
    user_settings.save_profile(user.id, body.model_dump())
    return body


@router.get("/user/apply/context")
def apply_context(
    url: str = Query(max_length=2000), user: AuthedUser = Depends(require_user)
) -> ApplyContext:
    """What this person already has for the page in front of them, before
    anything is filled: the profile facts that are set, the answer bank, and
    the drafts for the posting when the url matches one.

    A read. It opens no fill and writes nothing, which is what lets the
    panel show its three sources the moment it appears rather than naming
    them and making the person open the site to see what is in them.

    One call rather than three: the panel wants all of it at once, and the
    job match is the only way to know whether there are drafts at all.
    """
    job = db.query_one_as(
        MatchedPosting,
        f"SELECT j.id, j.company, j.title, uj.status, {applications.applied_on('uj')} AS date_applied, "
        "(SELECT MAX(f.submitted_at) FROM application_fills f "
        " WHERE f.user_id = %s AND f.job_id = j.id) AS submitted_at "
        "FROM jobs j LEFT JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = %s "
        "WHERE j.url = ANY(%s) "
        "AND j.id NOT IN (SELECT job_id FROM posting_uploads WHERE uploaded_by <> %s) LIMIT 1",
        (user.id, user.id, posting_urls(url), user.id),
    )
    # Only the facts that are set. A blank fact fills nothing, so listing it
    # would be a row that says the profile has something it does not.
    facts = {
        key: value
        for key, value in apply.load_profile(user.id)
        .model_dump(exclude={"default_resume_id", "notes"})
        .items()
        if value not in ("", None, [], {})
    }
    return ApplyContext(
        job=job,
        profile=facts,
        answers=db.query_as(
            RememberedAnswer,
            f"SELECT {_BANK_COLS} FROM application_answer_bank WHERE user_id = %s "
            "ORDER BY times_used DESC, updated_at DESC LIMIT 200",
            (user.id,),
        ),
        drafts=db.query_as(
            ContextDraft,
            "SELECT key, question, draft FROM application_answers "
            "WHERE user_id = %s AND job_id = %s AND draft IS NOT NULL ORDER BY id",
            (user.id, job.id),
        )
        if job
        else [],
    )


class SavedAnswerPut(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


@router.get("/user/answers")
def list_answers(user: AuthedUser = Depends(require_user)) -> AnswerList:
    return AnswerList(
        answers=db.query_as(
            RememberedAnswer,
            f"SELECT {_BANK_COLS} FROM application_answer_bank WHERE user_id = %s "
            "ORDER BY times_used DESC, updated_at DESC",
            (user.id,),
        )
    )


@router.put("/user/answers/{answer_id}")
def put_answer(
    answer_id: int, body: SavedAnswerPut, user: AuthedUser = Depends(require_user)
) -> RememberedAnswer:
    row = db.query_one_as(
        RememberedAnswer,
        f"UPDATE application_answer_bank SET value = %s, updated_at = now() "
        f"WHERE id = %s AND user_id = %s RETURNING {_BANK_COLS}",
        (body.value.strip(), answer_id, user.id),
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown answer")
    return row


@router.delete("/user/answers/{answer_id}")
def delete_answer(answer_id: int, user: AuthedUser = Depends(require_user)) -> Ok:
    if not db.query_one(
        "DELETE FROM application_answer_bank WHERE id = %s AND user_id = %s RETURNING id",
        (answer_id, user.id),
    ):
        raise refuse(404, "NOT_FOUND", "unknown answer")
    return Ok()
