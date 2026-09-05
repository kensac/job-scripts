"""Saved views: a named state of one list page, per user.

A view is what a person clicks to get a page back the way they left it:
the filters, the sort order across columns, the columns, the search. Several
per page, one default, ordered by position. The state is opaque here except
for its size; the frontend stores the page\'s canonical request shape, the
same names the API echoes in `filters`, `sorts` and `sortable`, so a view
is reproducible and never depends on a build\'s private wording.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser, require_user

router = APIRouter(prefix="/user/views")

# A state is a page's request parameters, not its data; 32 KB is fifty
# columns and a hundred filter values with room to spare.
_MAX_STATE_BYTES = 32_000
_COLS = "id, page, name, state, is_default, position, created_at, updated_at"


class ViewCreate(BaseModel):
    page: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    state: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class ViewPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    state: dict[str, Any] | None = None
    is_default: bool | None = None
    position: int | None = Field(default=None, ge=0)


def _check_state(state: dict[str, Any]) -> None:
    if len(json.dumps(state)) > _MAX_STATE_BYTES:
        raise HTTPException(
            400,
            detail={
                "code": "STATE_TOO_LARGE",
                "message": f"state must be under {_MAX_STATE_BYTES} bytes",
            },
        )


def _own(view_id: int, user: AuthedUser) -> dict[str, Any]:
    row = db.query_one(
        f"SELECT {_COLS} FROM saved_views WHERE id = %s AND user_id = %s", (view_id, user.id)
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown view"})
    return row


def _clear_default(user_id: int, page: str, keep: int | None) -> None:
    db.execute(
        "UPDATE saved_views SET is_default = false WHERE user_id = %s AND page = %s AND id IS DISTINCT FROM %s",
        (user_id, page, keep),
    )


@router.get("")
def list_views(page: str | None = None, user: AuthedUser = Depends(require_user)):
    """Every view of the caller's, optionally one page's, in switcher order."""
    rows = db.query(
        f"SELECT {_COLS} FROM saved_views WHERE user_id = %(uid)s "
        "AND (%(page)s::text IS NULL OR page = %(page)s) ORDER BY page, position, id",
        {"uid": user.id, "page": page},
    )
    return {"views": rows}


@router.post("", status_code=201)
def create_view(body: ViewCreate, user: AuthedUser = Depends(require_user)):
    _check_state(body.state)
    if body.is_default:
        _clear_default(user.id, body.page, None)
    try:
        row = db.query_one(
            f"""
            INSERT INTO saved_views (user_id, page, name, state, is_default, position)
            VALUES (%(uid)s, %(page)s, %(name)s, %(state)s, %(default)s,
                    (SELECT COALESCE(MAX(position), -1) + 1 FROM saved_views
                     WHERE user_id = %(uid)s AND page = %(page)s))
            RETURNING {_COLS}
            """,
            {
                "uid": user.id,
                "page": body.page,
                "name": body.name.strip(),
                "state": db.jsonb(body.state),
                "default": body.is_default,
            },
        )
    except UniqueViolation:
        raise HTTPException(
            409,
            detail={
                "code": "DUPLICATE_NAME",
                "message": f"a view named {body.name!r} already exists on {body.page}",
            },
        ) from None
    return row


@router.patch("/{view_id}")
def patch_view(view_id: int, body: ViewPatch, user: AuthedUser = Depends(require_user)):
    """Rename, restate, reorder, or make default; a field left out is left alone."""
    current = _own(view_id, user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    if "state" in fields:
        _check_state(fields["state"])
        fields["state"] = db.jsonb(fields["state"])
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    if fields.get("is_default"):
        _clear_default(user.id, current["page"], view_id)
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    try:
        row = db.query_one(
            f"UPDATE saved_views SET {cols}, updated_at = now() WHERE id = %(id)s AND user_id = %(uid)s "
            f"RETURNING {_COLS}",
            {**fields, "id": view_id, "uid": user.id},
        )
    except UniqueViolation:
        raise HTTPException(
            409,
            detail={
                "code": "DUPLICATE_NAME",
                "message": "a view with that name already exists on this page",
            },
        ) from None
    return row


@router.delete("/{view_id}")
def delete_view(view_id: int, user: AuthedUser = Depends(require_user)):
    _own(view_id, user)
    db.execute("DELETE FROM saved_views WHERE id = %s AND user_id = %s", (view_id, user.id))
    return {"deleted": view_id}
