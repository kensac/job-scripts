"""The person-owned state written over a catalog posting."""

from __future__ import annotations

import datetime

from api import db, events
from api.mail import applications

# The legacy row is machine-shaped only when every person-editable field still
# has its exact default. Kept here so migration tasks and live board writers
# classify the same row without importing one task handler from another.
UNTOUCHED = f"""
    (uj.status IS NULL OR uj.status = '') AND {applications.applied_on("uj")} IS NULL
    AND COALESCE(uj.notes, '') = '' AND COALESCE(uj.size, '') = ''
    AND COALESCE(uj.recruiter, '') = '' AND COALESCE(uj.connection1, '') = ''
    AND COALESCE(uj.connection2, '') = '' AND COALESCE(uj.documents, '') = ''
    AND NOT uj.hidden
"""

# A row a person acted on: stamped by a person write, or carrying a value only
# a person sets. A legacy all-default unstamped row is working-set membership
# (phase 2b), not this.
PERSON_STATE = f"(uj.person_touched_at IS NOT NULL OR NOT ({UNTOUCHED}))"


def board_row(user_id: int, job_id: int) -> dict:
    """The status, applied day and hidden flag of one board row as they now
    stand: what an open board is told when the row changes."""
    return (
        db.query_one(
            f"SELECT uj.status, {applications.applied_on('uj')} AS date_applied, uj.hidden "
            "FROM user_jobs uj WHERE uj.user_id = %s AND uj.job_id = %s",
            (user_id, job_id),
        )
        or {}
    )


def track_board_row(user_id: int, job_id: int) -> dict:
    """Record tracking intent without changing application status or dates."""
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, person_touched_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (user_id, job_id) DO UPDATE SET person_touched_at = now(), updated_at = now()",
        (user_id, job_id),
    )
    return board_row(user_id, job_id)


def touchable_job_ids(user_id: int, job_ids: list[int]) -> set[int]:
    """The ids this user may write a board row for.

    Pinning an unsubscribed job by patching it is a deliberate feature (the
    "watching" case). But a user_jobs row IS a visibility grant - visibility.FULL
    trusts a row the person acted on unconditionally - so an unrestricted pin
    launders around every other gate: pin, then read the job's cached page
    through /detail. The public catalog is fine to pin; another user's
    private upload is not, and that is the only distinction that matters.
    """
    rows = db.query(
        "SELECT id FROM jobs WHERE id = ANY(%s) AND (uploaded_by IS NULL OR uploaded_by = %s)",
        (job_ids, user_id),
    )
    return {row["id"] for row in rows}


def write_board_row(user_id: int, job_id: int, patch: dict, *, publish: bool = True) -> dict:
    """Applies one patch to one board row; returns what it filled in itself.
    publish=False is for a bulk caller that will publish once for all its
    rows: the per-row publish is a synchronous post, and the bulk endpoint
    exists so a large selection is one request."""
    fields = dict(patch)
    # The applied day belongs to the application, not to the board row.
    day_given = "date_applied" in fields
    day = fields.pop("date_applied", None)
    autofilled = {}
    existing = None
    if "status" in fields:
        existing = db.query_one(
            "SELECT status FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
    # Moving a row into an applied status dates its application once, so
    # nobody has to fill the day in by hand.
    if fields.get("status") in applications.APPLIED_STATUSES and not day_given:
        if applications.board_day(user_id, job_id) is None:
            # UTC, not the container's local date. The containers run
            # TZ=America/New_York, so date.today() silently decided
            # "today" in Eastern for every user regardless of theirs.
            day = datetime.datetime.now(datetime.UTC).date()
            day_given = True
            autofilled["date_applied"] = day.isoformat()
    if "status" in fields:
        old_status = existing["status"] if existing else None
        if (old_status or "") != (fields["status"] or ""):
            db.execute(
                "INSERT INTO user_job_history (user_id, job_id, old_status, new_status) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, job_id, old_status, fields["status"]),
            )
    insert_cols = "".join(f", {key}" for key in fields)
    insert_vals = "".join(f", %({key})s" for key in fields)
    cols = "".join(f"{key} = %({key})s, " for key in fields)
    with db.transaction():
        written = db.query_one(
            f"""
            INSERT INTO user_jobs (user_id, job_id, person_touched_at{insert_cols})
            VALUES (%(uid)s, %(jid)s, now(){insert_vals})
            ON CONFLICT (user_id, job_id) DO UPDATE SET
                {cols}person_touched_at = now(), updated_at = now()
            RETURNING status
            """,
            {"uid": user_id, "jid": job_id, **fields},
        )
        # A day the person gave, or a row moving into an applied status, is
        # the person saying they applied, and the application is recorded
        # with it, not by a sweep. Clearing the day keeps the application.
        if day_given and day is not None:
            applications.from_board(user_id, job_id, day, set_date=True)
        else:
            if day_given:
                applications.clear_board_day(user_id, job_id)
            if written and written["status"] in applications.APPLIED_STATUSES:
                applications.from_board(user_id, job_id, None, set_date=False)
    # Every path that writes a board row ends here, so this is the one place
    # an open board learns of the change without a reload.
    if publish:
        events.publish_board_row(user_id, job_id, board_row(user_id, job_id))
    return autofilled
