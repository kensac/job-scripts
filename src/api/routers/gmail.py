"""Connect, inspect and disconnect a user's Gmail grant.

The browser never sees the client secret: it gets an authorization URL from
/authorize, Google redirects it to a Next.js page, and that page posts the code
back to /callback, where the exchange happens server-side.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import oauth
from api.auth import AuthedUser, require_user
from api.models import Ok
from api.problem import AI_REFUSALS

router = APIRouter(prefix="/user/gmail")


def require_connect_access(user: AuthedUser = Depends(require_user)) -> AuthedUser:
    if not oauth.connect_allowed(user.groups):
        raise HTTPException(
            403,
            detail={
                "code": "GMAIL_CONNECT_DISABLED",
                "message": "mailbox connection is not enabled for your groups",
            },
        )
    return user


class AuthorizeRequest(BaseModel):
    redirect_uri: str | None = Field(default=None, max_length=500)


class CallbackRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=4096)


class GmailStatus(BaseModel):
    """The grant as the settings page renders it. `available` is about the
    caller's groups and the rest is about the grant, which is why an
    unavailable mailbox is still a 200 with connected=false."""

    available: bool
    connected: bool
    provider: str
    account_email: str | None
    scopes: list[str]
    needs_reconnect: bool
    invalid_reason: str | None
    connected_at: datetime.datetime | None


class AuthorizationUrl(BaseModel):
    authorization_url: str


@router.get("/status")
def gmail_status(user: AuthedUser = Depends(require_user)) -> GmailStatus:
    """Deliberately not gated: a user outside the allowed groups gets
    available=false rather than a 403, so the settings page can render the
    feature as unavailable instead of erroring."""
    return GmailStatus(available=oauth.connect_allowed(user.groups), **oauth.status(user.id))


@router.post("/authorize")
def authorize(
    body: AuthorizeRequest, user: AuthedUser = Depends(require_connect_access)
) -> AuthorizationUrl:
    try:
        url = oauth.authorization_url(user_id=user.id, redirect_uri=body.redirect_uri)
    except oauth.StateInvalid as exc:
        raise HTTPException(
            400, detail={"code": "INVALID_REDIRECT_URI", "message": str(exc)}
        ) from exc
    return AuthorizationUrl(authorization_url=url)


@router.post("/callback", responses=AI_REFUSALS)
def callback(
    body: CallbackRequest, user: AuthedUser = Depends(require_connect_access)
) -> GmailStatus:
    try:
        oauth.exchange_code(user_id=user.id, code=body.code, state=body.state)
    except oauth.StateInvalid as exc:
        raise HTTPException(400, detail={"code": "INVALID_STATE", "message": str(exc)}) from exc
    except oauth.ScopeDeclined as exc:
        raise HTTPException(400, detail={"code": "SCOPE_DECLINED", "message": str(exc)}) from exc
    except oauth.ProviderError as exc:
        raise HTTPException(502, detail={"code": "PROVIDER_ERROR", "message": str(exc)}) from exc
    return GmailStatus(available=True, **oauth.status(user.id))


@router.delete("")
def disconnect(user: AuthedUser = Depends(require_user)) -> Ok:
    """Gated on being signed in, not on the feature flag: someone who connected
    a mailbox must still be able to revoke it after the flag is narrowed."""
    # False is a real answer here: there was no grant to revoke.
    return Ok(ok=oauth.disconnect(user.id))
