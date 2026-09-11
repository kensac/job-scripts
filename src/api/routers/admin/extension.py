"""The capture recipes the browser extension fetches."""

from __future__ import annotations

import datetime
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, JsonValue

from api.apply import recipes as extension_recipes
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin

router = APIRouter()


class RecipePut(BaseModel):
    recipe: JsonValue


class RecipeRevision(BaseModel):
    """One publish. The recipe body itself is not here: this is the history
    list, and a body is tens of kilobytes that only the editor opens.
    `bytes` is its size, which is what the list has a use for."""

    adapter: str
    revision: str
    enabled: bool
    published_by: str | None
    published_at: datetime.datetime
    bytes: int


class RecipeHistory(BaseModel):
    recipes: list[RecipeRevision]


class RecipePublished(BaseModel):
    """The revision now serving that adapter. A revision is derived from the
    body, so publishing the same recipe twice returns the same one."""

    adapter: str
    revision: str


@router.get("/extension/recipes")
def list_extension_recipes(user: AuthedUser = Depends(require_admin)) -> RecipeHistory:
    """Every publish, newest first per adapter; the enabled row is what the
    extension fetches (api.apply.recipes)."""
    return RecipeHistory(recipes=[RecipeRevision(**r) for r in extension_recipes.history()])


@router.put("/extension/recipes/{adapter}")
def publish_extension_recipe(
    adapter: str, body: RecipePut, user: AuthedUser = Depends(require_admin)
) -> RecipePublished:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", adapter):
        raise HTTPException(400, detail={"code": "INVALID_RECIPE", "message": "bad adapter id"})
    try:
        revision = extension_recipes.publish(adapter, body.recipe, user.email or user.sub)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "INVALID_RECIPE", "message": str(exc)}) from exc
    return RecipePublished(adapter=adapter, revision=revision)


@router.post("/extension/recipes/{adapter}/rollback")
def rollback_extension_recipe(
    adapter: str, user: AuthedUser = Depends(require_admin)
) -> RecipePublished:
    revision = extension_recipes.rollback(adapter)
    if revision is None:
        raise HTTPException(
            404, detail={"code": "NO_PREVIOUS_RECIPE", "message": "nothing earlier to go back to"}
        )
    return RecipePublished(adapter=adapter, revision=revision)
