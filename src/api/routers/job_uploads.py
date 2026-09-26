"""A person's direct additions to the job catalog."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import job_imports
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
        saved = job_imports.save_posting(user.id, url, raw)
        job_imports.publish_saved(user.id, saved)
        accepted.append(AcceptedUpload(job_id=saved["job_id"], url=url))
    return Uploaded(accepted=accepted, rejected=rejected)
