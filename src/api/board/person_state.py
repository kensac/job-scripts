"""The person-owned state written over a catalog posting."""

from __future__ import annotations

import datetime

from api import db, events


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
    autofilled = {}
    existing = None
    if "status" in fields or "date_applied" not in fields:
        existing = db.query_one(
            "SELECT status, date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
    # Setting any real status implies the user acted on the job; stamp
    # date_applied once so they never have to fill it by hand.
    if fields.get("status") and "date_applied" not in fields:
        if not existing or existing["date_applied"] is None:
            # UTC, not the container's local date. The containers run
            # TZ=America/New_York, so date.today() silently decided
            # "today" in Eastern for every user regardless of theirs.
            fields["date_applied"] = datetime.datetime.now(datetime.UTC).date()
            autofilled["date_applied"] = fields["date_applied"].isoformat()
    if "status" in fields:
        old_status = existing["status"] if existing else None
        if (old_status or "") != (fields["status"] or ""):
            db.execute(
                "INSERT INTO user_job_history (user_id, job_id, old_status, new_status) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, job_id, old_status, fields["status"]),
            )
    cols = ", ".join(f"{key} = %({key})s" for key in fields)
    insert_cols = ", ".join(fields)
    insert_vals = ", ".join(f"%({key})s" for key in fields)
    written = db.query_one(
        f"""
        INSERT INTO user_jobs (user_id, job_id, {insert_cols})
        VALUES (%(uid)s, %(jid)s, {insert_vals})
        ON CONFLICT (user_id, job_id) DO UPDATE SET {cols}, updated_at = now()
        RETURNING status, date_applied, hidden
        """,
        {"uid": user_id, "jid": job_id, **fields},
    )
    # Every path that writes a board row ends here, so this is the one place
    # an open board learns of the change without a reload.
    if publish:
        events.publish_board_row(user_id, job_id, written or fields)
    return autofilled
