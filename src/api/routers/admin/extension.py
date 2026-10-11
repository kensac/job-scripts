"""The capture recipes the browser extension fetches."""

from __future__ import annotations

import datetime
import re
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api import db, queue
from api.apply import recipes as extension_recipes
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin.shared import require_admin

router = APIRouter()


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


class RecipeUploadStart(BaseModel):
    id: uuid.UUID
    adapter: str
    chunk_count: int = Field(gt=0)


class RecipeUploadReceipt(BaseModel):
    id: uuid.UUID
    status: str
    revision: str | None
    error: str | None
    received_chunks: int
    chunk_count: int


def _upload_receipt(upload_id: uuid.UUID) -> RecipeUploadReceipt:
    row = db.query_one(
        "SELECT id, status, revision, error, chunk_count, "
        "(SELECT count(*) FROM extension_recipe_chunks WHERE upload_id = u.id) AS received_chunks "
        "FROM extension_recipe_uploads u WHERE id = %s",
        (upload_id,),
    )
    if row is None:
        raise refuse(404, "NOT_FOUND", "unknown recipe upload")
    return RecipeUploadReceipt(**row)


@router.post("/extension/recipe-uploads", status_code=201)
def start_recipe_upload(
    body: RecipeUploadStart, user: AuthedUser = Depends(require_admin)
) -> RecipeUploadReceipt:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", body.adapter):
        raise refuse(400, "INVALID_RECIPE", "bad adapter id")
    with db.transaction():
        existing = db.query_one(
            "SELECT adapter, chunk_count FROM extension_recipe_uploads WHERE id = %s FOR UPDATE",
            (body.id,),
        )
        if existing is None:
            db.execute(
                "INSERT INTO extension_recipe_uploads "
                "(id, adapter, chunk_count, published_by, status) "
                "VALUES (%s, %s, %s, %s, 'receiving')",
                (body.id, body.adapter, body.chunk_count, user.email or user.sub),
            )
        elif (existing["adapter"], existing["chunk_count"]) != (body.adapter, body.chunk_count):
            raise refuse(409, "UPLOAD_CONFLICT", "recipe upload details changed")
    return _upload_receipt(body.id)


@router.get("/extension/recipe-uploads/{upload_id}")
def get_recipe_upload(
    upload_id: uuid.UUID, user: AuthedUser = Depends(require_admin)
) -> RecipeUploadReceipt:
    return _upload_receipt(upload_id)


@router.put("/extension/recipe-uploads/{upload_id}/chunks/{chunk_index}")
async def put_recipe_chunk(
    upload_id: uuid.UUID,
    chunk_index: int,
    request: Request,
    user: AuthedUser = Depends(require_admin),
) -> RecipeUploadReceipt:
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > extension_recipes.CHUNK_BYTES:
            raise refuse(413, "RECIPE_CHUNK_TOO_LARGE", "recipe chunk exceeds 64 KiB")
        data.extend(part)
    if not data:
        raise refuse(400, "EMPTY_CHUNK", "recipe chunk is empty")
    with db.transaction():
        upload = db.query_one(
            "SELECT chunk_count, status FROM extension_recipe_uploads WHERE id = %s FOR UPDATE",
            (upload_id,),
        )
        if upload is None:
            raise refuse(404, "NOT_FOUND", "unknown recipe upload")
        if chunk_index < 0 or chunk_index >= upload["chunk_count"]:
            raise refuse(400, "INVALID_CHUNK", "chunk index is outside this upload")
        if chunk_index < upload["chunk_count"] - 1 and len(data) != extension_recipes.CHUNK_BYTES:
            raise refuse(400, "INVALID_CHUNK", "non-final recipe chunks must be 64 KiB")
        existing = db.query_one(
            "SELECT body FROM extension_recipe_chunks WHERE upload_id = %s AND chunk_index = %s",
            (upload_id, chunk_index),
        )
        if existing is not None:
            if bytes(existing["body"]) != data:
                raise refuse(409, "CHUNK_CONFLICT", "chunk bytes changed")
        elif upload["status"] != "receiving":
            raise refuse(409, "UPLOAD_CLOSED", "recipe upload is no longer receiving")
        else:
            db.execute(
                "INSERT INTO extension_recipe_chunks (upload_id, chunk_index, body) "
                "VALUES (%s, %s, %s)",
                (upload_id, chunk_index, bytes(data)),
            )
    return _upload_receipt(upload_id)


@router.post("/extension/recipe-uploads/{upload_id}/complete")
def complete_recipe_upload(
    upload_id: uuid.UUID, user: AuthedUser = Depends(require_admin)
) -> RecipeUploadReceipt:
    with db.transaction():
        upload = db.query_one(
            "SELECT status, chunk_count, next_chunk FROM extension_recipe_uploads WHERE id = %s FOR UPDATE",
            (upload_id,),
        )
        if upload is None:
            raise refuse(404, "NOT_FOUND", "unknown recipe upload")
        if upload["status"] == "receiving":
            receipt = _upload_receipt(upload_id)
            if receipt.received_chunks != upload["chunk_count"]:
                raise refuse(409, "INCOMPLETE_RECIPE", "recipe chunks are still missing")
            db.execute(
                "UPDATE extension_recipe_uploads SET status = 'pending' WHERE id = %s",
                (upload_id,),
            )
        if upload["status"] in ("receiving", "pending"):
            active = db.query_one(
                "SELECT id FROM tasks WHERE kind = 'publish_extension_recipe' "
                "AND payload->>'upload_id' = %s "
                "AND status IN ('pending', 'running', 'waiting', 'parked') LIMIT 1",
                (str(upload_id),),
            )
            if active is None:
                queue.enqueue(
                    "publish_extension_recipe",
                    {"upload_id": str(upload_id), "chunk_index": upload["next_chunk"]},
                )
    return _upload_receipt(upload_id)


@router.get("/extension/recipes")
def list_extension_recipes(user: AuthedUser = Depends(require_admin)) -> RecipeHistory:
    """Every publish, newest first per adapter; the enabled row is what the
    extension fetches (api.apply.recipes)."""
    return RecipeHistory(recipes=[RecipeRevision(**r) for r in extension_recipes.history()])


@router.post("/extension/recipes/{adapter}/rollback")
def rollback_extension_recipe(
    adapter: str, user: AuthedUser = Depends(require_admin)
) -> RecipePublished:
    revision = extension_recipes.rollback(adapter)
    if revision is None:
        raise refuse(404, "NO_PREVIOUS_RECIPE", "nothing earlier to go back to")
    return RecipePublished(adapter=adapter, revision=revision)
