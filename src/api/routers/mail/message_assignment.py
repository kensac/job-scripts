"""Candidate lookup and user assignment of mail to applications."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.mail import applications
from api.mail import match as mail_match
from api.mail.store import owned_message
from api.problem import refuse
from api.routers.mail.conversation_identity import CONVERSATION_KEY_SQL, MAX_THREAD_FANOUT
from api.routers.mail.shared import Candidates, _candidates_payload

router = APIRouter()


class Assignment(BaseModel):
    """One of three targets. An application to attach to, a board job to
    create an application from, or a company and title when neither exists -
    which is the common case for mail predating the catalog."""

    application_id: int | None = None
    job_id: int | None = None
    company_name: str | None = None
    title: str | None = None
    note: str | None = None
    # Whether to carry the whole conversation. Default on, because a person
    # correcting one message of a thread means the thread, and making them do
    # it five times is the kind of chore this system exists to remove.
    whole_thread: bool = True


@router.get("/user/messages/{message_id}/candidates")
def match_candidates(
    message_id: int,
    q: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthedUser = Depends(require_user),
) -> Candidates:
    """What this message could belong to, best guesses first.

    The default order is not a search ranking, it is the matcher's own
    reasoning made visible. `_by_company` REFUSES when two applications at one
    employer are both plausible, and those rejected candidates are exactly
    what a person should be shown first - the system already knows the answer
    is one of them and only declined to guess which.

    Board jobs with no application are included because the correction a user
    most often wants is "this belongs to a job I tracked but never recorded
    applying to", and there is nothing to attach to until one exists.
    """
    message = owned_message(message_id, user.id)
    return _candidates_payload(message, user.id, q, limit)


class MessageAssigned(BaseModel):
    """What the assignment wrote. `messages_assigned` is how many moved: one
    click that quietly reassigns a dozen messages should report it rather than
    have the count discovered later."""

    ok: bool
    application_id: int
    messages_assigned: int


@router.post("/user/messages/{message_id}/assign")
def assign_message(
    message_id: int, body: Assignment, user: AuthedUser = Depends(require_user)
) -> MessageAssigned:
    """Attach this message to an application, creating one if asked.

    Appends. The previous match stays in the log and stops counting by
    latest-wins, so a correction never destroys the evidence for the decision
    it is correcting.

    A job_id creates the application from the tracked posting; a bare company
    and title creates a job-less one, which is the normal shape for mail
    predating the catalog. Never the other way round: an email does not get to
    invent a `jobs` row.
    """
    owned_message(message_id, user.id)
    application_id = body.application_id

    if application_id is not None:
        owned = db.query_one(
            "SELECT id FROM applications WHERE id = %s AND user_id = %s",
            (application_id, user.id),
        )
        if owned is None:
            raise refuse(404, "NOT_FOUND", "application not found")
    elif body.job_id is not None:
        job = db.query_one(
            "SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user.id, body.job_id),
        )
        if job is None:
            raise refuse(404, "NOT_FOUND", "job not on your board")
        application_id = applications.from_board(user.id, body.job_id, None, set_date=False)
    elif body.company_name:
        created = db.query_one(
            "INSERT INTO applications (user_id, job_id, company_name, title, "
            "source_provenance, applied_at) VALUES (%s, NULL, %s, %s, 'manual', "
            "(SELECT sent_at FROM email_messages WHERE id = %s)) RETURNING id",
            (user.id, body.company_name, body.title, message_id),
        )
        assert created is not None
        application_id = created["id"]
    else:
        raise refuse(
            400,
            "TARGET_REQUIRED",
            "give an application_id, a job_id, or a company_name to create one",
        )

    # The provider's own thread id, never a derived one. Grouping threadless
    # mail by normalised subject and sender was measured and is unsafe: "thank
    # you for applying!" from myworkday.com is 49 messages from 49 DIFFERENT
    # employers, and merging those would attach 49 unrelated applications to
    # one. The correct signal is the References/In-Reply-To chain. Takeout
    # stores it (`headers` on all 38,190 Takeout rows, 2026-10-10) and takes
    # its thread id from it; .olm stores none (28,451 rows), so threadless mail
    # is assigned one message at a time.
    targets = [message_id]
    if body.whole_thread:
        siblings = db.query(
            f"""
            SELECT m.id FROM email_messages m
            WHERE m.user_id = %(user)s
              AND {CONVERSATION_KEY_SQL} = (
                  SELECT {CONVERSATION_KEY_SQL} FROM email_messages m WHERE m.id = %(msg)s
              )
              AND m.id <> %(msg)s
            ORDER BY m.id
            LIMIT %(cap)s
            """,
            {"user": user.id, "msg": message_id, "cap": MAX_THREAD_FANOUT},
        )
        targets.extend(r["id"] for r in siblings)

    rationale = body.note or "assigned by the user"
    for index, target in enumerate(targets):
        mail_match.record(
            target,
            mail_match.MatchDecision(
                application_id,
                mail_match.MANUAL,
                "high",
                rationale if index == 0 else f"{rationale} (same conversation)",
            ),
            actor_user_id=user.id,
        )
    return MessageAssigned(ok=True, application_id=application_id, messages_assigned=len(targets))
