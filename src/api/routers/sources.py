from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db, visibility
from api.auth import AuthedUser, require_user
from api.models import SourcesPut
from core import boards

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
    visibility.request_refresh(user.id)
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
    rows = db.query(
        """
        SELECT s.name, s.listings_url, s.description, s.company,
               us.user_id IS NOT NULL AS enabled,
               COALESCE((SELECT array_agg(g.name ORDER BY g.name) FROM source_groups g
                         WHERE g.active AND s.name = ANY(g.members)), '{}') AS groups
        FROM sources s
        LEFT JOIN user_sources us ON us.source = s.name AND us.user_id = %s
        WHERE s.active
        ORDER BY s.name
        """,
        (user.id,),
    )
    # The format is what a person groups 389 boards by, and the company is
    # what names one; both were admin-only until the subscribe page had to
    # show a wall of slugs.
    for r in rows:
        r["kind"] = boards.kind(r["listings_url"])
    return {"sources": rows}


class SourcesPatch(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


def _check_names(user_id: int, names: set[str], adding: set[str]) -> None:
    """Refuse a name no source has, and refuse a NEW subscription to a board
    that is switched off. A board a person already holds stays theirs after
    it is switched off: on 2026-09-05 eight feeds went inactive under a
    subscriber, and every save of that page was refused whole for naming
    them, so nothing could be saved until the switch was undone."""
    rows = db.query("SELECT name, active FROM sources WHERE name = ANY(%s)", (sorted(names),))
    known = {r["name"]: r["active"] for r in rows}
    unknown = sorted(n for n in names if n not in known)
    if unknown:
        raise HTTPException(
            400, detail={"code": "UNKNOWN_SOURCE", "message": f"unknown sources: {unknown}"}
        )
    held = {
        r["source"]
        for r in db.query("SELECT source FROM user_sources WHERE user_id = %s", (user_id,))
    }
    off = sorted(n for n in adding if not known[n] and n not in held)
    if off:
        raise HTTPException(
            400,
            detail={"code": "SOURCE_INACTIVE", "message": f"switched off, cannot subscribe: {off}"},
        )


@router.patch("/user/sources")
def patch_sources(body: SourcesPatch, user: AuthedUser = Depends(require_user)):
    """A delta: subscribe to these, unsubscribe from those. One toggle used to
    PUT the whole enabled set, which at 389 boards raced with itself and could
    not express "leave this bundle" at all. Names in both lists end up
    subscribed; an unknown name, or a new subscription to a switched-off
    board, refuses the write whole rather than applying half."""
    _check_names(user.id, set(body.add) | set(body.remove), set(body.add))
    with db.pool.connection() as conn:
        removed = conn.execute(
            "DELETE FROM user_sources WHERE user_id = %s AND source = ANY(%s) RETURNING source",
            (user.id, sorted(set(body.remove) - set(body.add))),
        ).fetchall()
        added = conn.execute(
            "INSERT INTO user_sources (user_id, source) SELECT %s, unnest(%s::text[]) "
            "ON CONFLICT DO NOTHING RETURNING source",
            (user.id, sorted(set(body.add))),
        ).fetchall()
    enabled = [
        r["source"]
        for r in db.query(
            "SELECT source FROM user_sources WHERE user_id = %s ORDER BY source", (user.id,)
        )
    ]
    visibility.request_refresh(user.id)
    return {
        "ok": True,
        "added": sorted(r["source"] for r in added),
        "removed": sorted(r["source"] for r in removed),
        "enabled": enabled,
    }


@router.put("/user/sources")
def put_sources(body: SourcesPut, user: AuthedUser = Depends(require_user)):
    _check_names(user.id, set(body.enabled), set(body.enabled))
    with db.pool.connection() as conn:
        conn.execute("DELETE FROM user_sources WHERE user_id = %s", (user.id,))
        for source in set(body.enabled):
            conn.execute(
                "INSERT INTO user_sources (user_id, source) VALUES (%s, %s)",
                (user.id, source),
            )
    visibility.request_refresh(user.id)
    return {"ok": True}
