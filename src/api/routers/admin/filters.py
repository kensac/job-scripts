"""The shared preset library, and starting a person's filter run."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin

router = APIRouter()


class PresetBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    on_ambiguous: str | None = None
    fail_closed: bool | None = None
    active: bool | None = None


@router.get("/filter-presets")
def admin_list_presets(user: AuthedUser = Depends(require_admin)):
    return {"presets": db.query("SELECT * FROM filter_presets ORDER BY name")}


class FilterRunBody(BaseModel):
    user_id: int
    # One filter, or every enabled filter of the person when absent.
    filter_id: int | None = None
    # Past the shared weekly cap, for this run only; the spend is recorded.
    ignore_budget: bool = False


@router.post("/filters/run")
def admin_run_filter(body: FilterRunBody, user: AuthedUser = Depends(require_admin)):
    """Queue a filter run for a person. With ignore_budget the run goes past
    the shared weekly cap for that run alone, so the cap never has to be
    raised and put back (Kanishk, 2026-09-08). The person's own run endpoint
    cannot set the flag."""
    from api import filter_runs

    if not db.query_one("SELECT 1 FROM users WHERE id = %s", (body.user_id,)):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown user"})
    if body.filter_id is not None and not db.query_one(
        "SELECT 1 FROM user_filters WHERE id = %s AND user_id = %s",
        (body.filter_id, body.user_id),
    ):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
    result = filter_runs.enqueue(
        body.user_id, body.filter_id, policy="interactive", ignore_budget=body.ignore_budget
    )
    if result.conflict:
        raise HTTPException(
            409,
            detail={
                "code": "IN_PROGRESS",
                "message": "this run is already in progress",
                "task_id": result.conflict.id,
            },
        )
    task_id = result.task_id
    return {"task_id": task_id, "ignore_budget": body.ignore_budget}


@router.post("/filter-presets")
def create_preset(body: PresetBody, user: AuthedUser = Depends(require_admin)):
    if not body.name or not body.prompt:
        raise HTTPException(
            400, detail={"code": "MISSING_FIELDS", "message": "name and prompt are required"}
        )
    if db.query_one("SELECT id FROM filter_presets WHERE name = %s", (body.name,)):
        raise HTTPException(409, detail={"code": "DUPLICATE_NAME", "message": "preset name exists"})
    return db.query_one(
        """
        INSERT INTO filter_presets (name, description, prompt, on_ambiguous, fail_closed, active)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
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


@router.patch("/filter-presets/{preset_id}")
def patch_preset(preset_id: int, body: PresetBody, user: AuthedUser = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one(
        f"UPDATE filter_presets SET {cols}, updated_at = now() WHERE id = %(pid)s RETURNING *",
        {"pid": preset_id, **fields},
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown preset"})
    return row


@router.delete("/filter-presets/{preset_id}")
def delete_preset(preset_id: int, user: AuthedUser = Depends(require_admin)):
    db.execute("DELETE FROM filter_presets WHERE id = %s", (preset_id,))
    return {"ok": True}
