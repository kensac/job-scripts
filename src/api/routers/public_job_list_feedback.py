"""What an anonymous visitor to a public list can tell the administrator.

A note about the list as a whole is a `job_list_feedback` row. A report about
one posting on it is a `reports` row with no person and the list named instead,
so it lands in the posting queue the administrator already works and can be
answered with the same actions. Neither is a verdict: an anonymous report never
queues a recheck, because the fleet's spend would then be set by whoever
submits the form. The administrator reads it and acts.

Nothing here authenticates, and nothing here may: any authenticated request
provisions a user row. The only identity a row carries is the optional
`contact` the visitor typed.
"""

from __future__ import annotations

import datetime
import re

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from api import db
from api.problem import refuse
from api.reports import REPORT_KINDS

router = APIRouter(prefix="/public/job-lists", tags=["public-job-lists"])

# What one hand-written note runs to with room to spare; a longer body is a
# paste, and the request is refused rather than the text cut.
MESSAGE_MAX = 4000
# RFC 5321's limit on a forward path, which is the longest address a reply
# could be sent to.
CONTACT_MAX = 254
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# One key for every intake, so two requests cannot both read the count below
# the limit and both insert.
_INTAKE_LOCK = "public-job-list-feedback-intake"


def _contact(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    address = value.strip()
    if not _ADDRESS.fullmatch(address):
        raise ValueError("contact must be an email address")
    return address


class ListFeedback(BaseModel):
    message: str = Field(max_length=MESSAGE_MAX)
    contact: str | None = Field(default=None, max_length=CONTACT_MAX)

    @field_validator("message")
    @classmethod
    def _message_says_something(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be empty")
        return value.strip()

    _check_contact = field_validator("contact")(_contact)


class PublicJobReport(BaseModel):
    kind: str
    message: str = Field(default="", max_length=MESSAGE_MAX)
    contact: str | None = Field(default=None, max_length=CONTACT_MAX)

    _check_contact = field_validator("contact")(_contact)


class FeedbackReceived(BaseModel):
    id: int
    created_at: datetime.datetime


def _published_board(slug: str) -> int:
    row = db.query_one("SELECT id FROM managed_boards WHERE slug = %s AND published", (slug,))
    if row is None:
        raise refuse(404, "NOT_FOUND", "unknown job list")
    return row["id"]


def _admit() -> None:
    """Refuse when the hour's anonymous intake, across every list, is at the
    administrator's limit. Call inside the transaction that inserts, so the
    lock holds until the row is visible to the next caller's count."""
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_INTAKE_LOCK,))
    limit = db.get_config("public_feedback_hourly_limit")
    if limit == 0:
        raise refuse(503, "FEEDBACK_CLOSED", "feedback is not being accepted right now")
    row = db.query_one(
        """
        SELECT (SELECT count(*) FROM job_list_feedback
                WHERE created_at > now() - interval '1 hour')
             + (SELECT count(*) FROM reports
                WHERE user_id IS NULL AND created_at > now() - interval '1 hour') AS n
        """
    )
    assert row is not None  # an aggregate with no GROUP BY is one row
    if row["n"] >= limit:
        raise refuse(
            429, "FEEDBACK_BUSY", "too much feedback arrived in the last hour; try again later"
        )


@router.post("/{slug}/feedback")
def leave_list_feedback(slug: str, body: ListFeedback) -> FeedbackReceived:
    board_id = _published_board(slug)
    with db.transaction():
        _admit()
        row = db.query_one_as(
            FeedbackReceived,
            "INSERT INTO job_list_feedback (managed_board_id, message, contact) "
            "VALUES (%s, %s, %s) RETURNING id, created_at",
            (board_id, body.message, body.contact),
        )
    assert row is not None  # an insert with RETURNING always yields its row
    return row


@router.post("/{slug}/jobs/{job_id}/report")
def report_public_job(slug: str, job_id: int, body: PublicJobReport) -> FeedbackReceived:
    if body.kind not in REPORT_KINDS:
        raise refuse(400, "INVALID_KIND", f"kind must be one of {REPORT_KINDS}")
    if body.kind == "other" and not body.message.strip():
        raise refuse(400, "MESSAGE_REQUIRED", "say what is wrong with this posting")
    board = db.query_one(
        """
        SELECT b.id FROM managed_board_jobs mj
        JOIN managed_boards b ON b.id = mj.managed_board_id AND b.published
        WHERE b.slug = %s AND mj.job_id = %s
        """,
        (slug, job_id),
    )
    if board is None:
        raise refuse(404, "NOT_FOUND", "unknown job")
    with db.transaction():
        _admit()
        row = db.query_one_as(
            FeedbackReceived,
            "INSERT INTO reports (managed_board_id, job_id, kind, message, contact) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at",
            (board["id"], job_id, body.kind, body.message.strip(), body.contact),
        )
    assert row is not None  # an insert with RETURNING always yields its row
    return row
