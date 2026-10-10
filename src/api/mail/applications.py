"""`applications` is the one record that a person applied.

Three writers, one per provenance: the board (`tracker`, when a row moves
into an applied status), the extension's submit (`apply`, for a form whose
posting is not on the board), and the mail matcher (`email`, in
tasks/mail_match.seed_from_mail). The board and the submit write it in the
transaction that records the act, so there is no sweep that catches up.
"""

from __future__ import annotations

import datetime

from api import db

# The board statuses that mean an application exists. "No Longer Interested"
# is excluded: it is the status this user assigns to postings they decided
# against, and 634 of them would otherwise become applications never made.
APPLIED_STATUSES = ("Application Submitted", "Follow-up")

TRACKER = "tracker"
APPLY = "apply"


def from_board(user_id: int, job_id: int, applied_on: datetime.date | None) -> int:
    """The application for a board row in an applied status, created if
    there is none. Company and title are copied rather than joined at read
    time because `applications.job_id` is ON DELETE SET NULL, and an
    application that loses its posting must not also lose its identity.

    A date is a day in no recorded timezone; stored as its UTC midnight, as
    the matcher's tracker slack expects (api/mail/match._by_company)."""
    row = db.query_one(
        """
        WITH created AS (
            INSERT INTO applications (user_id, job_id, company_name, title,
                                      source_provenance, applied_at)
            SELECT %(user)s, j.id, j.company, j.title, %(tracker)s, %(applied)s
            FROM jobs j WHERE j.id = %(job)s
            ON CONFLICT (user_id, job_id) WHERE job_id IS NOT NULL DO NOTHING
            RETURNING id
        )
        SELECT id FROM created
        UNION ALL
        SELECT id FROM applications WHERE user_id = %(user)s AND job_id = %(job)s
        LIMIT 1
        """,
        {"user": user_id, "job": job_id, "tracker": TRACKER, "applied": applied_on},
    )
    assert row is not None
    return row["id"]


def from_form(user_id: int, board: str | None, submitted_at: datetime.datetime) -> int:
    """An application for a submitted form whose posting is not on the board.
    `board` is the employer's board name from the form url, the best name the
    submit has; the matcher compares names loosely enough that 'stripe' meets
    'Stripe, Inc.'."""
    row = db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, source_provenance, "
        "applied_at) VALUES (%s, NULL, %s, %s, %s) RETURNING id",
        (user_id, board, APPLY, submitted_at),
    )
    assert row is not None
    return row["id"]
