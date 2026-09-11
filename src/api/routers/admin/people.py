"""Who the users are, what they may spend, and how a new one gets in."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db, sorting
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin

router = APIRouter()


logger = logging.getLogger(__name__)


# Whitelisted sort keys for the users ledger; the SQL names the computed
# columns of the query below, so a key cannot reach anything the row does not
# already show.
_USERS_SORTABLE = {
    "last_seen_at": "u.last_seen_at",
    "created_at": "u.created_at",
    "email": "lower(u.email)",
    "name": "lower(u.name)",
    "board_rows": "board_rows",
    "enabled_filters": "enabled_filters",
    "sources": "sources",
    "owner_tokens_week": "owner_tokens_week",
}


@router.get("/users")
def list_users(
    limit: int = 50,
    offset: int = 0,
    sort: str = "last_seen_at",
    dir: str = "desc",
    ids: str | None = None,
    user: AuthedUser = Depends(require_admin),
):
    limit = max(1, min(limit, 200))
    sorts = sorting.parse(sort, dir, _USERS_SORTABLE, "last_seen_at")
    # ids= is a bulk lookup: the queue page names workers' tasks by user and
    # was walking up to 40 pages to build that map. Ids that are not integers
    # are ignored rather than refused, so a malformed selection returns what
    # it can.
    wanted = [int(x) for x in (ids or "").split(",") if x.strip().lstrip("-").isdigit()]
    scope = "WHERE u.id = ANY(%(ids)s)" if ids is not None else ""
    rows = db.query(
        f"""
        SELECT u.id, u.sub, u.email, u.name, u.groups, u.created_at, u.last_seen_at,
               s.api_key_enc IS NOT NULL AS has_byo_key,
               s.ai_provider, s.ai_model, s.bypass_sponsorship_filter,
               (SELECT COUNT(*) FROM user_jobs uj WHERE uj.user_id = u.id) AS board_rows,
               (SELECT COUNT(*) FROM user_filters uf
                WHERE uf.user_id = u.id AND uf.enabled) AS enabled_filters,
               (SELECT COUNT(*) FROM user_sources us WHERE us.user_id = u.id) AS sources,
               COALESCE((SELECT SUM(a.total_tokens) FROM api_usage a
                         WHERE a.user_id = u.id AND a.key_source = 'owner'
                           AND a.created_at > now() - interval '7 days'), 0) AS owner_tokens_week
        FROM users u LEFT JOIN user_settings s ON s.user_id = u.id
        {scope}
        ORDER BY {sorting.clause(sorts, _USERS_SORTABLE)}, u.id LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"limit": limit + 1, "offset": max(0, offset), "ids": wanted},
    )
    return {
        "users": rows[:limit],
        "has_more": len(rows) > limit,
        # Echoed and enumerated for the same reason as /admin/jobs: the page
        # renders the active sort without duplicating the default and never
        # has to guess the accepted keys.
        "sort": sorts[0]["key"],
        "dir": sorts[0]["dir"],
        "sorts": sorts,
        "sortable": sorted(_USERS_SORTABLE),
    }


@router.get("/users/{user_id}")
def user_detail(user_id: int, user: AuthedUser = Depends(require_admin)):
    u = db.query_one(
        """
        SELECT u.id, u.sub, u.email, u.name, u.groups, u.created_at, u.last_seen_at,
               s.ai_provider, s.ai_model, s.ai_params, s.bypass_sponsorship_filter,
               s.criteria, s.api_key_enc IS NOT NULL AS has_byo_key
        FROM users u LEFT JOIN user_settings s ON s.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    if not u:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown user"})
    from api import budget as _budget

    # The Users page showed a weekly token total one click from a page showing
    # a 5,000,000 budget, with nothing saying which cap applied to whom - which
    # invited the reading that the owner was over budget when his group is
    # uncapped. Resolve it here rather than leaving the UI to infer.
    groups = u.get("groups") or []
    owner_key, weekly_cap = _budget._owner_budget(groups)
    granting = db.query(
        "SELECT group_name, weekly_token_budget FROM group_budgets "
        "WHERE group_name = ANY(%s) ORDER BY group_name",
        (groups,),
    )
    return {
        "user": u,
        "budget": {
            "owner_key": owner_key,
            # None means uncapped, which is a real answer and not a missing one.
            "weekly_token_budget": weekly_cap,
            "spent_this_week": _budget.spent_this_week(user_id) if owner_key else 0,
            "granted_by": granting,
        },
        "spend_by_day": db.query(
            """
            SELECT created_at::date AS day, key_source,
                   SUM(total_tokens) AS tokens, COUNT(*) AS calls
            FROM api_usage WHERE user_id = %s AND created_at > now() - interval '30 days'
            GROUP BY 1, 2 ORDER BY 1
            """,
            (user_id,),
        ),
        "spend_by_purpose": db.query(
            """
            SELECT purpose, model, SUM(total_tokens) AS tokens, COUNT(*) AS calls
            FROM api_usage WHERE user_id = %s GROUP BY 1, 2 ORDER BY 3 DESC
            """,
            (user_id,),
        ),
        "board": db.query(
            """
            SELECT COALESCE(NULLIF(status, ''), 'not_applied') AS status,
                   COUNT(*) AS count, COUNT(*) FILTER (WHERE hidden) AS hidden
            FROM user_jobs WHERE user_id = %s GROUP BY 1 ORDER BY 2 DESC
            """,
            (user_id,),
        ),
        "filters": db.query(
            "SELECT id, name, enabled, on_ambiguous, fail_closed, updated_at "
            "FROM user_filters WHERE user_id = %s ORDER BY id",
            (user_id,),
        ),
        "sources": [
            r["source"]
            for r in db.query(
                "SELECT source FROM user_sources WHERE user_id = %s ORDER BY source",
                (user_id,),
            )
        ],
        "uploads": db.query_one(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE extraction_status = 'failed') AS failed "
            "FROM jobs WHERE uploaded_by = %s",
            (user_id,),
        ),
        "reports": db.query_one(
            "SELECT COUNT(*) FILTER (WHERE status = 'open') AS open, COUNT(*) AS total "
            "FROM reports WHERE user_id = %s",
            (user_id,),
        ),
        "recent_tasks": db.query(
            """
            SELECT id, kind, status, worker, created_at, finished_at
            FROM tasks WHERE payload->>'user_id' = %s ORDER BY id DESC LIMIT 10
            """,
            (str(user_id),),
        ),
    }


AUTHENTIK_URL = os.environ.get("AUTHENTIK_URL", "").rstrip("/")


AUTHENTIK_INVITE_TOKEN = os.environ.get("AUTHENTIK_INVITE_TOKEN", "")


AUTHENTIK_INVITE_FLOW = os.environ.get("AUTHENTIK_INVITE_FLOW", "jobtracker-enrollment")


# The invite service account can't read flows (403 by design), so the flow is
# addressed by its UUID, not resolved by slug.
AUTHENTIK_INVITE_FLOW_PK = os.environ.get(
    "AUTHENTIK_INVITE_FLOW_PK", "ecb38a8d-47a5-4eb5-afd1-fb2a480d144e"
)


def _invites_configured() -> bool:
    return bool(AUTHENTIK_URL and AUTHENTIK_INVITE_TOKEN and AUTHENTIK_INVITE_FLOW_PK)


def _authentik_client():
    import httpx

    return httpx.Client(
        base_url=f"{AUTHENTIK_URL}/api/v3",
        headers={"Authorization": f"Bearer {AUTHENTIK_INVITE_TOKEN}"},
        timeout=15,
    )


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/invites")
def create_invite(body: InviteBody, user: AuthedUser = Depends(require_admin)):
    """Email-only onboarding: creates a single-use Authentik invitation bound
    to the jobtracker enrollment flow and emails the link. The invitee picks
    their own username/name/password during enrollment."""
    if not _invites_configured():
        raise HTTPException(
            503,
            detail={"code": "INVITES_NOT_CONFIGURED", "message": "authentik invite env missing"},
        )
    import datetime as _dt
    import re as _re

    from api import mail

    email = body.email.strip().lower()
    expires = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=7)).isoformat()
    slug = _re.sub(r"[^a-z0-9]+", "-", email).strip("-")
    with _authentik_client() as ak:
        resp = ak.post(
            "/stages/invitation/invitations/",
            json={
                "name": f"jobtracker-{slug}-{int(_dt.datetime.now(_dt.UTC).timestamp())}",
                "expires": expires,
                "fixed_data": {"email": email},
                "single_use": True,
                "flow": AUTHENTIK_INVITE_FLOW_PK,
            },
        )
        if resp.status_code >= 300:
            raise HTTPException(
                502,
                detail={
                    "code": "AUTHENTIK_ERROR",
                    "message": f"invitation create failed ({resp.status_code})",
                },
            )
        inv = resp.json()
    invite_url = f"{AUTHENTIK_URL}/if/flow/{AUTHENTIK_INVITE_FLOW}/?itoken={inv['pk']}"
    emailed = False
    if mail.configured():
        try:
            mail.send_invite(email, invite_url)
            emailed = True
        except Exception:
            # The invite itself succeeded; only delivery failed. Report it
            # rather than leaving emailed=False unexplained.
            logger.exception(f"invite created but email to {email} failed")
    return {
        "ok": True,
        "invite_url": invite_url,
        "expires": expires,
        "emailed": emailed,
        "pk": inv["pk"],
    }


@router.get("/invites")
def list_invites(user: AuthedUser = Depends(require_admin)):
    if not _invites_configured():
        return {"rows": [], "configured": False}
    with _authentik_client() as ak:
        resp = ak.get(
            "/stages/invitation/invitations/", params={"flow__slug": AUTHENTIK_INVITE_FLOW}
        )
        if resp.status_code >= 300:
            raise HTTPException(
                502, detail={"code": "AUTHENTIK_ERROR", "message": "invitation list failed"}
            )
        data = resp.json()
    rows = [
        {
            "pk": r["pk"],
            "email": (r.get("fixed_data") or {}).get("email", ""),
            "expires": r.get("expires"),
            "single_use": r.get("single_use", True),
        }
        for r in data.get("results", [])
    ]
    return {"rows": rows, "configured": True}


@router.delete("/invites/{pk}")
def revoke_invite(pk: str, user: AuthedUser = Depends(require_admin)):
    if not _invites_configured():
        raise HTTPException(
            503,
            detail={"code": "INVITES_NOT_CONFIGURED", "message": "authentik invite env missing"},
        )
    with _authentik_client() as ak:
        resp = ak.delete(f"/stages/invitation/invitations/{pk}/")
        if resp.status_code >= 300 and resp.status_code != 404:
            raise HTTPException(
                502, detail={"code": "AUTHENTIK_ERROR", "message": "invitation revoke failed"}
            )
    return {"ok": True}


class GroupBudgetPut(BaseModel):
    weekly_token_budget: int | None = Field(default=None, ge=0)
    allowed_models: list[str] | None = Field(default=None, max_length=50)


@router.get("/group-budgets")
def list_group_budgets(user: AuthedUser = Depends(require_admin)):
    from api import ai

    return {
        "groups": db.query(
            "SELECT group_name, weekly_token_budget, allowed_models "
            "FROM group_budgets ORDER BY group_name"
        ),
        "catalog_models": sorted(
            m["model"] for models in ai.MODEL_CATALOG.values() for m in models
        ),
    }


@router.put("/group-budgets/{group_name}")
def put_group_budget(
    group_name: str, body: GroupBudgetPut, user: AuthedUser = Depends(require_admin)
):
    from api import ai

    if body.allowed_models is not None:
        known = {m["model"] for models in ai.MODEL_CATALOG.values() for m in models}
        unknown = [m for m in body.allowed_models if m not in known]
        if unknown:
            raise HTTPException(
                400,
                detail={"code": "UNKNOWN_MODEL", "message": f"unknown models: {unknown}"},
            )
    db.execute(
        """
        INSERT INTO group_budgets (group_name, weekly_token_budget, allowed_models)
        VALUES (%s, %s, %s)
        ON CONFLICT (group_name) DO UPDATE SET
            weekly_token_budget = EXCLUDED.weekly_token_budget,
            allowed_models = EXCLUDED.allowed_models
        """,
        (group_name, body.weekly_token_budget, body.allowed_models),
    )
    return {
        "group_name": group_name,
        "weekly_token_budget": body.weekly_token_budget,
        "allowed_models": body.allowed_models,
    }


@router.delete("/group-budgets/{group_name}")
def delete_group_budget(group_name: str, user: AuthedUser = Depends(require_admin)):
    db.execute("DELETE FROM group_budgets WHERE group_name = %s", (group_name,))
    return {"ok": True}
