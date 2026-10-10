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


def applied_on(alias: str) -> str:
    """The day a person applied to the posting on board row `alias`, as SQL.

    A tracker application stores its day as that day's UTC midnight
    (from_board), so this is its UTC calendar date. Applications the
    matcher or a submitted form recorded have no posting, so a board row
    only ever finds its own tracker application here."""
    return (
        "(SELECT (a.applied_at AT TIME ZONE 'UTC')::date FROM applications a "
        f"WHERE a.user_id = {alias}.user_id AND a.job_id = {alias}.job_id)"
    )


def board_day(user_id: int, job_id: int) -> datetime.date | None:
    """The applied day on a board row's application; None when there is no
    application or it has no day."""
    row = db.query_one(
        "SELECT (applied_at AT TIME ZONE 'UTC')::date AS day FROM applications "
        "WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )
    return row["day"] if row else None


def clear_board_day(user_id: int, job_id: int) -> None:
    """The person cleared the applied day: the application stays, undated."""
    db.execute(
        "UPDATE applications SET applied_at = NULL, updated_at = now() "
        "WHERE user_id = %s AND job_id = %s AND applied_at IS NOT NULL",
        (user_id, job_id),
    )


def from_board(user_id: int, job_id: int, applied: datetime.date | None, *, set_date: bool) -> int:
    """The application for a board row, created if there is none. With
    set_date the day on an existing application is replaced by `applied`
    (the person edited it); without, an existing one is left as it is.
    Company and title are copied rather than joined at read time because
    `applications.job_id` is ON DELETE SET NULL, and an application that
    loses its posting must not also lose its identity.

    A day is in no recorded timezone; stored as its UTC midnight, as the
    matcher's tracker slack expects (api/mail/match._by_company)."""
    on_conflict = (
        "DO UPDATE SET applied_at = EXCLUDED.applied_at, updated_at = now()"
        if set_date
        else "DO NOTHING"
    )
    row = db.query_one(
        f"""
        WITH created AS (
            INSERT INTO applications (user_id, job_id, company_name, title,
                                      source_provenance, applied_at)
            SELECT %(user)s, j.id, j.company, j.title, %(tracker)s,
                   %(applied)s::date::timestamp AT TIME ZONE 'UTC'
            FROM jobs j WHERE j.id = %(job)s
            ON CONFLICT (user_id, job_id) WHERE job_id IS NOT NULL {on_conflict}
            RETURNING id
        )
        SELECT id FROM created
        UNION ALL
        SELECT id FROM applications WHERE user_id = %(user)s AND job_id = %(job)s
        LIMIT 1
        """,
        {"user": user_id, "job": job_id, "tracker": TRACKER, "applied": applied},
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
