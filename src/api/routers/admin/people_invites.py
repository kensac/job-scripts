"""Authentik invitations for administrator-managed enrollment."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.auth import AuthedUser
from api.models import Ok
from api.problem import PROVIDER_REFUSALS, refuse
from api.routers.admin.shared import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)

AUTHENTIK_URL = os.environ.get("AUTHENTIK_URL", "").rstrip("/")


AUTHENTIK_INVITE_TOKEN = os.environ.get("AUTHENTIK_INVITE_TOKEN", "")


AUTHENTIK_INVITE_FLOW = os.environ.get("AUTHENTIK_INVITE_FLOW", "jobtracker-enrollment")


# The invite service account can't read flows (403 by design), so the flow is
# addressed by its UUID, not resolved by slug.
AUTHENTIK_INVITE_FLOW_PK = os.environ.get(
    "AUTHENTIK_INVITE_FLOW_PK", "ecb38a8d-47a5-4eb5-afd1-fb2a480d144e"
)


def _invitation_service_is_configured() -> bool:
    return bool(AUTHENTIK_URL and AUTHENTIK_INVITE_TOKEN and AUTHENTIK_INVITE_FLOW_PK)


def _invitation_api_client():
    import httpx

    return httpx.Client(
        base_url=f"{AUTHENTIK_URL}/api/v3",
        headers={"Authorization": f"Bearer {AUTHENTIK_INVITE_TOKEN}"},
        timeout=15,
    )


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InviteCreated(BaseModel):
    """`emailed` is false when the invite was created but delivery failed, so
    the link is returned either way and somebody can send it by hand."""

    ok: bool
    invite_url: str
    expires: str
    emailed: bool
    pk: str


@router.post("/invites", responses=PROVIDER_REFUSALS)
def create_invite(body: InviteBody, user: AuthedUser = Depends(require_admin)) -> InviteCreated:
    """Email-only onboarding: creates a single-use Authentik invitation bound
    to the jobtracker enrollment flow and emails the link. The invitee picks
    their own username/name/password during enrollment."""
    if not _invitation_service_is_configured():
        raise refuse(503, "INVITES_NOT_CONFIGURED", "authentik invite env missing")
    import datetime as _dt
    import re as _re

    from api import mail

    email = body.email.strip().lower()
    expires = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=7)).isoformat()
    slug = _re.sub(r"[^a-z0-9]+", "-", email).strip("-")
    with _invitation_api_client() as invitation_client:
        response = invitation_client.post(
            "/stages/invitation/invitations/",
            json={
                "name": f"jobtracker-{slug}-{int(_dt.datetime.now(_dt.UTC).timestamp())}",
                "expires": expires,
                "fixed_data": {"email": email},
                "single_use": True,
                "flow": AUTHENTIK_INVITE_FLOW_PK,
            },
        )
        if response.status_code >= 300:
            raise refuse(
                502, "AUTHENTIK_ERROR", f"invitation create failed ({response.status_code})"
            )
        created_invitation = response.json()
    invite_url = (
        f"{AUTHENTIK_URL}/if/flow/{AUTHENTIK_INVITE_FLOW}/?itoken={created_invitation['pk']}"
    )
    emailed = False
    if mail.configured():
        try:
            mail.send_invite(email, invite_url)
            emailed = True
        except Exception:
            # The invite itself succeeded; only delivery failed. Report it
            # rather than leaving emailed=False unexplained.
            logger.exception(f"invite created but email to {email} failed")
    return InviteCreated(
        ok=True,
        invite_url=invite_url,
        expires=expires,
        emailed=emailed,
        pk=created_invitation["pk"],
    )


class Invite(BaseModel):
    """An outstanding invitation, as Authentik holds it. `pk` is Authentik's
    id and is what revoking one names."""

    pk: str
    email: str
    expires: str | None
    single_use: bool


class InviteList(BaseModel):
    """`configured` is false when the Authentik environment is missing, which
    is why the list is empty rather than there being nothing outstanding."""

    rows: list[Invite]
    configured: bool


@router.get("/invites", responses=PROVIDER_REFUSALS)
def list_invites(user: AuthedUser = Depends(require_admin)) -> InviteList:
    if not _invitation_service_is_configured():
        return InviteList(rows=[], configured=False)
    with _invitation_api_client() as invitation_client:
        response = invitation_client.get(
            "/stages/invitation/invitations/", params={"flow__slug": AUTHENTIK_INVITE_FLOW}
        )
        if response.status_code >= 300:
            raise refuse(502, "AUTHENTIK_ERROR", "invitation list failed")
        data = response.json()
    return InviteList(
        rows=[
            Invite(
                pk=r["pk"],
                email=(r.get("fixed_data") or {}).get("email", ""),
                expires=r.get("expires"),
                single_use=r.get("single_use", True),
            )
            for r in data.get("results", [])
        ],
        configured=True,
    )


@router.delete("/invites/{pk}", responses=PROVIDER_REFUSALS)
def revoke_invite(pk: str, user: AuthedUser = Depends(require_admin)) -> Ok:
    if not _invitation_service_is_configured():
        raise refuse(503, "INVITES_NOT_CONFIGURED", "authentik invite env missing")
    with _invitation_api_client() as invitation_client:
        response = invitation_client.delete(f"/stages/invitation/invitations/{pk}/")
        if response.status_code >= 300 and response.status_code != 404:
            raise refuse(502, "AUTHENTIK_ERROR", "invitation revoke failed")
    return Ok()
