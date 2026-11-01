"""The shared preset library."""

from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.models import Ok
from api.problem import refuse
from api.routers.admin.shared import require_admin
from api.updates import NonNullUpdate

router = APIRouter()


# Every column of filter_presets, named once. Three routes return a whole
# preset row and all three read this; the star select they used before could
# not be typed and could not say what it returned.
_PRESET_COLS = (
    "id, name, description, prompt, on_ambiguous, fail_closed, active, created_at, updated_at"
)


class PresetBody(BaseModel):
    name: NonNullUpdate[Annotated[str, Field(min_length=1, max_length=80)]] = None
    description: NonNullUpdate[Annotated[str, Field(max_length=500)]] = None
    prompt: NonNullUpdate[Annotated[str, Field(min_length=1, max_length=8000)]] = None
    on_ambiguous: NonNullUpdate[str] = None
    fail_closed: NonNullUpdate[bool] = None
    active: NonNullUpdate[bool] = None


class FilterPreset(BaseModel):
    """A preset as stored: a prompt somebody can start a filter from, shared
    across everyone rather than owned by one person."""

    id: int
    name: str
    description: str
    prompt: str
    on_ambiguous: str
    fail_closed: bool
    active: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime


class FilterPresets(BaseModel):
    presets: list[FilterPreset]


@router.get("/filter-presets")
def admin_list_presets(user: AuthedUser = Depends(require_admin)) -> FilterPresets:
    return FilterPresets(
        presets=db.query_as(
            FilterPreset, f"SELECT {_PRESET_COLS} FROM filter_presets ORDER BY name"
        )
    )


@router.post("/filter-presets")
def create_preset(body: PresetBody, user: AuthedUser = Depends(require_admin)) -> FilterPreset:
    if not body.name or not body.prompt:
        raise refuse(400, "MISSING_FIELDS", "name and prompt are required")
    if db.query_one("SELECT id FROM filter_presets WHERE name = %s", (body.name,)):
        raise refuse(409, "DUPLICATE_NAME", "preset name exists")
    row = db.query_one_as(
        FilterPreset,
        f"""
        INSERT INTO filter_presets (name, description, prompt, on_ambiguous, fail_closed, active)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_PRESET_COLS}
        """,
        (
            body.name,
            body.description or "",
            body.prompt,
            body.on_ambiguous or "keep",
            bool(body.fail_closed),
            body.active if body.active is not None else True,
        ),
    )
    assert row is not None
    return row


@router.patch("/filter-presets/{preset_id}")
def patch_preset(
    preset_id: int, body: PresetBody, user: AuthedUser = Depends(require_admin)
) -> FilterPreset:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise refuse(400, "EMPTY_PATCH", "no fields to update")
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one_as(
        FilterPreset,
        f"UPDATE filter_presets SET {cols}, updated_at = now() "
        f"WHERE id = %(pid)s RETURNING {_PRESET_COLS}",
        {"pid": preset_id, **fields},
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown preset")
    return row


@router.delete("/filter-presets/{preset_id}")
def delete_preset(preset_id: int, user: AuthedUser = Depends(require_admin)) -> Ok:
    db.execute("DELETE FROM filter_presets WHERE id = %s", (preset_id,))
    return Ok()
