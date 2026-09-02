"""Connect, inspect and disconnect a user's Gmail grant.

The browser never sees the client secret: it gets an authorization URL from
/authorize, Google redirects it to a Next.js page, and that page posts the code
back to /callback, where the exchange happens server-side.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import oauth
from api.auth import AuthedUser, require_user

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


@router.get("/status")
def gmail_status(user: AuthedUser = Depends(require_user)):
    """Deliberately not gated: a user outside the allowed groups gets
    available=false rather than a 403, so the settings page can render the
    feature as unavailable instead of erroring."""
    return {"available": oauth.connect_allowed(user.groups), **oauth.status(user.id)}


@router.post("/authorize")
def authorize(body: AuthorizeRequest, user: AuthedUser = Depends(require_connect_access)):
    try:
        url = oauth.authorization_url(user_id=user.id, redirect_uri=body.redirect_uri)
    except oauth.StateInvalid as exc:
        raise HTTPException(
            400, detail={"code": "INVALID_REDIRECT_URI", "message": str(exc)}
        ) from exc
    return {"authorization_url": url}


@router.post("/callback")
def callback(body: CallbackRequest, user: AuthedUser = Depends(require_connect_access)):
    try:
        oauth.exchange_code(user_id=user.id, code=body.code, state=body.state)
    except oauth.StateInvalid as exc:
        raise HTTPException(400, detail={"code": "INVALID_STATE", "message": str(exc)}) from exc
    except oauth.ScopeDeclined as exc:
        raise HTTPException(400, detail={"code": "SCOPE_DECLINED", "message": str(exc)}) from exc
    except oauth.ProviderError as exc:
        raise HTTPException(502, detail={"code": "PROVIDER_ERROR", "message": str(exc)}) from exc
    return {"available": True, **oauth.status(user.id)}


@router.delete("")
def disconnect(user: AuthedUser = Depends(require_user)):
    """Gated on being signed in, not on the feature flag: someone who connected
    a mailbox must still be able to revoke it after the flag is narrowed."""
    return {"ok": oauth.disconnect(user.id)}
