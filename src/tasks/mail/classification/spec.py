"""Classification request instructions, schema, and message text."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

CLASSIFICATION_INSTRUCTIONS = """You classify a single email from a job seeker's mailbox.

Return the event kind that best describes what the SENDER is communicating.
Use "not_job_related" for anything that is not about a job application or a
recruiter approaching the candidate - newsletters, bank statements, receipts,
code review notifications, and so on. Most mail is not job related.

"recruiter_outreach" is cold contact about a role the candidate did not apply
to. It is NOT an update on an existing application.

For company and role, copy what the email states. If the email does not state
it, leave it null. Do NOT infer a company from the sender's domain and do NOT
guess a role title.

"offer" is an offer of EMPLOYMENT - a job, internship or paid position the
candidate can accept and be paid for. Nothing else is an offer, however it is
worded:

- An "offer of admission" to a university, degree or programme is NOT an offer,
  even when it names money. Tuition credits, enrolment deposits and
  scholarships are not pay.
- Acceptance into a course, a hackathon RSVP or team allocation, a club or
  conference allotment, and "you're in" or "confirm your spot" mail are
  "not_job_related".
- Discussing a job already started, or congratulating someone on one, is not
  an offer.

If the email is about paid work but does not extend an offer, use the kind
that fits what it does say.

For a deadline, only give a date the email actually states or plainly implies
("within 5 days" from a dated email is implied; "soon" is not). Leave it null
otherwise. Never invent one.
"""


class MailClassification(BaseModel):
    kind: Literal[
        "acknowledgement",
        "rejection",
        "assessment_invite",
        "interview_invite",
        "interview_scheduled",
        "info_request",
        "offer",
        "recruiter_outreach",
        "position_closed",
        "not_job_related",
    ]
    # Null means the email did not say. That is a different fact from "no
    # company", and collapsing the two is what makes a model's output
    # unusable for matching.
    company: str | None
    role_title: str | None
    # ISO date, or null. Never a guess.
    deadline: str | None
    deadline_is_explicit: bool
    confidence: Literal["high", "medium", "low"]


# Dates the model actually returns, measured over 200 real messages rather
# than guessed: "March 1, 2023", "Thursday, February 1, 2024",
# "Tue Dec 10, 2024 at 4:00PM (EST)". The instruction asks for ISO and the
# model frequently answers in prose, so accepting only ISO discarded 7.5% of
# the deadlines it found - real dates, thrown away.
def classification_request_text(row: dict[str, Any]) -> str:
    return (
        f"From: {row.get('from_email') or ''}\n"
        f"Subject: {row.get('subject') or ''}\n"
        f"Date: {row.get('sent_at') or ''}\n\n"
        f"{(row.get('body_text') or '')[:20000]}"
    )
