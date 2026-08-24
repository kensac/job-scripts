from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api import db
from api.auth import AuthedUser, require_user
from api.models import SourcesPut

router = APIRouter()


@router.get("/sources")
def list_sources(user: AuthedUser = Depends(require_user)):
    return {
        "sources": db.query(
            """
            SELECT s.name, s.listings_url,
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
    known = {
        r["name"] for r in db.query("SELECT name FROM sources WHERE active")
    }
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
