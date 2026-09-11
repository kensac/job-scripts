"""The capture recipes the browser extension fetches."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, JsonValue

from api.apply import recipes as extension_recipes
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin

router = APIRouter()


class RecipePut(BaseModel):
    recipe: JsonValue


@router.get("/extension/recipes")
def list_extension_recipes(user: AuthedUser = Depends(require_admin)):
    """Every publish, newest first per adapter; the enabled row is what the
    extension fetches (api.apply.recipes)."""
    return {"recipes": extension_recipes.history()}


@router.put("/extension/recipes/{adapter}")
def publish_extension_recipe(
    adapter: str, body: RecipePut, user: AuthedUser = Depends(require_admin)
):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", adapter):
        raise HTTPException(400, detail={"code": "INVALID_RECIPE", "message": "bad adapter id"})
    try:
        revision = extension_recipes.publish(adapter, body.recipe, user.email or user.sub)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "INVALID_RECIPE", "message": str(exc)}) from exc
    return {"adapter": adapter, "revision": revision}


@router.post("/extension/recipes/{adapter}/rollback")
def rollback_extension_recipe(adapter: str, user: AuthedUser = Depends(require_admin)):
    revision = extension_recipes.rollback(adapter)
    if revision is None:
        raise HTTPException(
            404, detail={"code": "NO_PREVIOUS_RECIPE", "message": "nothing earlier to go back to"}
        )
    return {"adapter": adapter, "revision": revision}
