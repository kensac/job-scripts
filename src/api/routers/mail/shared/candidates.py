"""The applications and board jobs a message could belong to."""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.resolve.choice_policy import by_company, choices_for_message, thread_size
from api.resolve.contracts import PICKER_APPLICATIONS, ResolveChoice


class CandidateMessage(BaseModel):
    """The message the picker is deciding about, with what the classifier read
    out of it - which is what the ranking below compared."""

    id: int
    subject: str | None
    from_email: str | None
    sent_at: datetime.datetime | None
    extracted_company: str | None
    extracted_title: str | None


class CandidateApplication(BaseModel):
    """One application this message could belong to, and why it is on the list.

    `reason` separates a candidate the matcher considered and declined to
    choose between from a search hit. A dismissed application is listed so the
    picker can show it and refused at the write, so `dismissed_at` is part of
    the row rather than a reason to omit it."""

    id: int
    job_id: int | None
    company_name: str | None
    title: str | None
    applied_at: datetime.datetime | None
    source_provenance: str
    dismissed_at: datetime.datetime | None
    board_status: str | None
    stage: str
    event_count: int
    reason: str


class CandidateJob(BaseModel):
    """A posting on the board with no application yet. Attaching mail to one
    creates the application, which is the correction people most often want."""

    id: int
    company: str | None
    title: str | None
    url: str | None
    status: str | None


class Candidates(BaseModel):
    message: CandidateMessage
    applications: list[CandidateApplication]
    # The verbs, decided by the server. A client reads
    # `payload[choice.target_source]`, which is why this payload's list is
    # called `applications` and the queue's is called `candidates`.
    #
    # A verb's optional fields arrive as null here rather than being omitted,
    # which is what they were. exclude_none is not available on this route -
    # the rest of the payload has real nulls and has always sent them - and a
    # serialiser that dropped them would erase ResolveChoice from the schema,
    # leaving the generated client an untyped object. So the null wins and the
    # consumer was checked: the picker reads `c.needs_target` and
    # `c.reason` as optional, and null is falsy exactly as absent was.
    choices: list[ResolveChoice]
    total_applications: int
    # The count the matcher choked on. Two or more means it refused on purpose
    # rather than finding nothing.
    same_company_candidates: int
    board_jobs: list[CandidateJob]


# Why a candidate is on the list at all, and the value `same_company_candidates`
# counts.
_SAME_COMPANY = "same company as this mail"


def candidates_payload(
    message: dict[str, Any], owner_id: int, q: str | None, limit: int
) -> Candidates:
    """Candidates for a message, scoped to the message's OWNER.

    Separated from the route because the admin view asks the same question
    about somebody else's mailbox. The owner is a parameter rather than the
    caller precisely so that difference is stated once instead of a second
    copy of the ranking drifting from this one.
    """
    message_id = message["id"]
    event = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    detail = (event or {}).get("detail") or {}
    company = detail.get("company")
    key = mail_match.norm_company(company)

    apps = db.query(
        """
        SELECT a.id, a.job_id, a.company_name, a.title, a.applied_at,
               a.source_provenance, a.dismissed_at, uj.status AS board_status
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        WHERE a.user_id = %s
        """,
        (owner_id,),
    )
    events = mail_pipeline.events_by_application(owner_id)
    needle = (q or "").lower().strip()
    # Ranked on a key that never reaches the response: same company first, then
    # alphabetically. It used to travel as a `_rank` field stripped on the way
    # out, which is a field the shape would now have to declare in order to
    # delete.
    scored: list[tuple[tuple[int, str], CandidateApplication]] = []
    for app in apps:
        haystack = f"{app['company_name'] or ''} {app['title'] or ''}".lower()
        if needle and needle not in haystack:
            continue
        same_company = bool(key) and mail_match.norm_company(app["company_name"]) == key
        own = events.get(app["id"], [])
        scored.append(
            (
                (0 if same_company else 1, app["company_name"] or ""),
                CandidateApplication(
                    **app,
                    stage=mail_pipeline.stage_for(own, app["board_status"]),
                    event_count=len(own),
                    # Why it is on the list at all. A candidate the matcher
                    # considered and declined to choose between is a different
                    # thing from a search hit, and the UI should be able to say so.
                    reason=_SAME_COMPANY if same_company else "search match",
                ),
            )
        )
    scored.sort(key=lambda r: r[0])
    ranked = [row for _, row in scored]
    ambiguous = sum(1 for row in ranked if row.reason == _SAME_COMPANY)

    jobs = db.query_as(
        CandidateJob,
        """
        SELECT j.id, j.company, j.title, j.url, uj.status
        FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %(user)s
          AND NOT EXISTS (
              SELECT 1 FROM applications a WHERE a.user_id = uj.user_id AND a.job_id = uj.job_id
          )
          AND (%(q)s::text IS NULL OR lower(j.company) LIKE %(like)s OR lower(j.title) LIKE %(like)s)
        ORDER BY uj.updated_at DESC, j.id DESC
        LIMIT %(limit)s
        """,
        {"user": owner_id, "q": q, "like": f"%{needle}%", "limit": limit},
    )
    # The verbs available here, decided by the server. The modal is the surface
    # a person actually makes this decision on, and it is reached from the mail
    # list and the unmatched queue rather than only from a queue page - so it
    # needs the same declared choices the queue rows carry, or its eligibility
    # is a client-side guess.
    choices = choices_for_message(
        # Only the undismissed ones decide eligibility. This list carries
        # dismissed applications so the picker can show them; assigning to
        # one is refused at the write, so they must not make "belongs to an
        # application" look available.
        by_company([a for a in apps if a["dismissed_at"] is None]),
        company,
        thread_size(owner_id, message.get("provider_thread_id")),
        # This payload calls its list `applications`, not `candidates`. A
        # client reads `payload[choice.target_source]`, so naming the queue's
        # key here would point it at a field this response does not have - the
        # hardcoded fact moved rather than removed.
        PICKER_APPLICATIONS,
    )
    return Candidates(
        message=CandidateMessage(
            id=message["id"],
            subject=message["subject"],
            from_email=message["from_email"],
            sent_at=message["sent_at"],
            extracted_company=company,
            extracted_title=detail.get("role_title"),
        ),
        applications=ranked[:limit],
        choices=[ResolveChoice.model_validate(choice) for choice in choices],
        total_applications=len(ranked),
        same_company_candidates=ambiguous,
        board_jobs=jobs,
    )
