"""A person's explicit additions to the catalog and their board."""

from api import db, events, task_admission
from api.board.person_state import track_board_row
from api.problem import refuse


def save_posting(user_id: int, url: str, raw_url: str) -> dict:
    """The caller validates the public URL before entering a transaction."""
    with db.transaction():
        row = db.query_one(
            "INSERT INTO jobs (url, raw_url, source, uploaded_by, extraction_status) "
            "VALUES (%s, %s, 'upload', %s, 'pending') "
            "ON CONFLICT (url) DO UPDATE SET extraction_status = "
            "CASE WHEN jobs.extraction_status = 'failed' THEN 'pending' ELSE jobs.extraction_status END "
            "RETURNING id, extraction_status, uploaded_by",
            (url, raw_url, user_id),
        )
        assert row
        if row["uploaded_by"] not in (None, user_id):
            raise refuse(403, "PRIVATE_POSTING", "This posting belongs to another user.")
        tracked = track_board_row(user_id, row["id"])
        if row["extraction_status"] == "pending":
            task_admission.enqueue("extract_upload", {"job_id": row["id"]}, {"user_id": user_id})
    return {"job_id": row["id"], "url": url, "state": tracked or {}}


def publish_saved(user_id: int, saved: dict) -> None:
    events.publish_board_row(user_id, saved["job_id"], saved["state"])
