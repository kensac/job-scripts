"""Keep the previous resume before a write replaces it."""

from __future__ import annotations

from api import db


def lock_owner(user_id: int) -> None:
    db.query_one("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))


def archive_resume(resume_id: int, user_id: int) -> None:
    db.execute(
        """INSERT INTO user_resume_revisions
           (user_id, resume_id, name, text, filename, pdf, pdf_upload_id, text_version_id)
           SELECT user_id, id, name, text, filename, pdf, pdf_upload_id, text_version_id
             FROM user_resumes WHERE id = %s AND user_id = %s""",
        (resume_id, user_id),
    )
