from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser, require_user
from api.models import SourcesPut

router = APIRouter()


@router.get("/source-groups")
def list_source_groups(user: AuthedUser = Depends(require_user)):
    enabled = {
        r["source"]
        for r in db.query("SELECT source FROM user_sources WHERE user_id = %s", (user.id,))
    }
    groups = db.query(
        "SELECT name, members, description FROM source_groups WHERE active ORDER BY name"
    )
    for g in groups:
        members = g["members"] or []
        g["subscribed"] = bool(members) and set(members).issubset(enabled)
    return {"groups": groups}


class ApplyGroupBody(BaseModel):
    name: str
    mode: str = "replace"


@router.post("/user/sources/apply-group")
def apply_source_group(body: ApplyGroupBody, user: AuthedUser = Depends(require_user)):
    if body.mode not in ("replace", "add"):
        raise HTTPException(
            400, detail={"code": "INVALID_MODE", "message": "mode must be replace or add"}
        )
    group = db.query_one(
        "SELECT members FROM source_groups WHERE name = %s AND active", (body.name,)
    )
    if not group:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown group"})
    members = [
        r["name"]
        for r in db.query(
            "SELECT name FROM sources WHERE active AND name = ANY(%s)",
            (group["members"] or [],),
        )
    ]
    with db.pool.connection() as conn:
        if body.mode == "replace":
            conn.execute("DELETE FROM user_sources WHERE user_id = %s", (user.id,))
        for source in members:
            conn.execute(
                "INSERT INTO user_sources (user_id, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user.id, source),
            )
    return {"ok": True, "enabled": members, "mode": body.mode}


class SourceRequestBody(BaseModel):
    url: str = Field(min_length=8, max_length=1000)
    note: str = Field(default="", max_length=2000)


@router.post("/user/source-requests")
def create_source_request(body: SourceRequestBody, user: AuthedUser = Depends(require_user)):
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(
            400, detail={"code": "INVALID_URL", "message": "the board link must be a URL"}
        )
    row = db.query_one(
        "INSERT INTO source_requests (user_id, url, note) VALUES (%s, %s, %s) "
        "RETURNING id, url, note, status, created_at",
        (user.id, body.url, body.note),
    )
    return row


@router.get("/user/source-requests")
def list_own_source_requests(user: AuthedUser = Depends(require_user)):
    return {
        "requests": db.query(
            "SELECT id, url, note, status, resolution_note, created_at, resolved_at "
            "FROM source_requests WHERE user_id = %s ORDER BY id DESC",
            (user.id,),
        )
    }


@router.get("/sources")
def list_sources(user: AuthedUser = Depends(require_user)):
    return {
        "sources": db.query(
            """
            SELECT s.name, s.listings_url, s.description,
                   us.user_id IS NOT NULL AS enabled
            FROM sources s
            LEFT JOIN user_sources us ON us.source = s.name AND us.user_id = %s
            WHERE s.active
            ORDER BY s.name
            """,
            (user.id,),
        )
    }


@router.put("/user/sources")
def put_sources(body: SourcesPut, user: AuthedUser = Depends(require_user)):
    known = {r["name"] for r in db.query("SELECT name FROM sources WHERE active")}
    unknown = [s for s in body.enabled if s not in known]
    if unknown:
        raise HTTPException(
            400, detail={"code": "UNKNOWN_SOURCE", "message": f"unknown sources: {unknown}"}
        )
    with db.pool.connection() as conn:
        conn.execute("DELETE FROM user_sources WHERE user_id = %s", (user.id,))
        for source in set(body.enabled):
            conn.execute(
                "INSERT INTO user_sources (user_id, source) VALUES (%s, %s)",
                (user.id, source),
            )
    return {"ok": True}
