"""A person's direct additions to the job catalog."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, task_admission
from api.auth import AuthedUser, require_user
from api.models import UploadRequest
from core.fetching.urls import normalize_url

router = APIRouter()


class AcceptedUpload(BaseModel):
    job_id: int
    url: str


class RejectedUpload(BaseModel):
    url: str
    error: str


class Uploaded(BaseModel):
    """Accepted and rejected separately, with the reason on each rejection: an
    upload is the one place a person chooses the url, so being told now beats
    a job that silently never extracts."""

    accepted: list[AcceptedUpload]
    rejected: list[RejectedUpload]


@router.post("/uploads")
def upload_links(body: UploadRequest, user: AuthedUser = Depends(require_user)) -> Uploaded:
    from api import ssrf

    accepted: list[AcceptedUpload] = []
    rejected: list[RejectedUpload] = []
    for submitted in body.urls:
        raw = submitted.strip()
        if not raw.startswith(("http://", "https://")):
            continue
        # Fail here as well as in the fetcher: an upload is the one place a
        # user chooses the URL, and rejecting it now gives them an answer
        # instead of a job that silently never extracts.
        error = ssrf.validate_public_url(raw)
        if error:
            rejected.append(RejectedUpload(url=raw, error=error))
            continue
        url = normalize_url(raw)
        row = db.query_one(
            """
            INSERT INTO jobs (url, raw_url, source, uploaded_by, extraction_status)
            VALUES (%s, %s, 'upload', %s, 'pending')
            ON CONFLICT (url) DO UPDATE SET
                extraction_status = CASE WHEN jobs.extraction_status = 'failed'
                                         THEN 'pending' ELSE jobs.extraction_status END
            RETURNING id, extraction_status
            """,
            (url, raw, user.id),
        )
        assert row is not None
        db.execute(
            "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user.id, row["id"]),
        )
        if row["extraction_status"] == "pending":
            task_admission.enqueue("extract_upload", {"job_id": row["id"]}, {"user_id": user.id})
        accepted.append(AcceptedUpload(job_id=row["id"], url=url))
    return Uploaded(accepted=accepted, rejected=rejected)
