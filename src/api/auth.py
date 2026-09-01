from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from typing import List

from fastapi import Header, HTTPException

from api import db, metrics

logger = logging.getLogger("jobtracker_api")

SERVICE_TOKEN = os.environ.get("JOBTRACKER_SERVICE_TOKEN", "")


@dataclass
class AuthedUser:
    id: int
    sub: str
    email: str
    name: str
    groups: List[str]


def require_service(x_service_token: str = Header(default="")) -> None:
    """Proxy-level auth only — for routes that identify their subject by a
    token in the request (e.g. one-click unsubscribe) rather than headers."""
    if not SERVICE_TOKEN or not hmac.compare_digest(x_service_token, SERVICE_TOKEN):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "invalid service token"})


def require_user(
    x_service_token: str = Header(default=""),
    x_user_sub: str = Header(default=""),
    x_user_email: str = Header(default=""),
    x_user_name: str = Header(default=""),
    x_user_groups: str = Header(default=""),
) -> AuthedUser:
    if not SERVICE_TOKEN or not hmac.compare_digest(x_service_token, SERVICE_TOKEN):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "invalid service token"})
    if not x_user_sub:
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "missing user subject"})
    groups = [g.strip() for g in x_user_groups.split(",") if g.strip()]
    existing = db.query_one("SELECT id FROM users WHERE sub = %s", (x_user_sub,))
    if existing is None and not db.get_config("signups_enabled", True):
        if not {"infra-admins", "jobtracker-users-internal"}.intersection(groups):
            raise HTTPException(
                403,
                detail={"code": "SIGNUPS_DISABLED", "message": "new signups are currently disabled"},
            )
    if existing is None and not (x_user_email or "").strip():
        # Provisioning a user is a real side effect, and this endpoint trusts
        # whatever identity the proxy forwards. A malformed or mistyped sub
        # would otherwise mint a phantom user row silently; every genuine
        # identity carries an email, so its absence means the request is wrong.
        raise HTTPException(
            400,
            detail={"code": "IDENTITY_INCOMPLETE",
                    "message": "cannot provision a user without an email"},
        )
    row = db.query_one(
        """
        INSERT INTO users (sub, email, name, groups)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (sub) DO UPDATE SET
            email = COALESCE(NULLIF(EXCLUDED.email, ''), users.email),
            name = COALESCE(NULLIF(EXCLUDED.name, ''), users.name),
            groups = EXCLUDED.groups,
            last_seen_at = now()
        RETURNING id, sub, email, name, groups, (xmax = 0) AS is_new
        """,
        (x_user_sub, x_user_email, x_user_name, groups),
    )
    assert row is not None
    if row["is_new"]:
        # Loud on purpose: a new user appearing is either a real signup or a
        # bug in whatever forwarded the identity, and both are worth seeing.
        metrics.USERS_PROVISIONED.inc()
        logger.warning(
            "provisioned new user id=%s email=%s groups=%s",
            row["id"], row["email"], groups,
        )
    return AuthedUser(
        id=row["id"],
        sub=row["sub"],
        email=row["email"] or "",
        name=row["name"] or "",
        groups=row["groups"] or [],
    )
