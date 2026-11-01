"""Validate a stored recipe chunk by chunk and publish only a complete revision."""

from __future__ import annotations

import hashlib
import uuid

from api import db
from api.apply.recipe_json import InvalidRecipe, RecipeParser

_SEED = bytes(32)
_DOMAIN = b"recipe-chunk-v2\0"


def advance_digest(previous: bytes, index: int, chunk: bytes) -> bytes:
    """Bind each chunk's position, length, and bytes into a rolling digest."""
    return hashlib.sha256(
        _DOMAIN
        + previous
        + index.to_bytes(8, "big")
        + len(chunk).to_bytes(8, "big")
        + hashlib.sha256(chunk).digest()
    ).digest()


def process(upload_id: uuid.UUID, index: int) -> None:
    """Validate one chunk and save the parser state and digest it leaves."""
    with db.transaction():
        upload = db.query_one(
            "SELECT adapter, chunk_count, published_by, status, next_chunk, validation_state "
            "FROM extension_recipe_uploads WHERE id = %s FOR UPDATE",
            (upload_id,),
        )
        if upload is None or upload["status"] != "pending" or upload["next_chunk"] != index:
            return
        row = db.query_one(
            "SELECT body FROM extension_recipe_chunks WHERE upload_id = %s AND chunk_index = %s",
            (upload_id, index),
        )
        if row is None:
            raise ValueError("recipe upload is incomplete")
        chunk = bytes(row["body"])
        previous_state = upload["validation_state"]
        digest = _SEED if previous_state is None else bytes.fromhex(previous_state["digest"])
        parser = RecipeParser(
            upload["adapter"],
            previous_state["parser"] if previous_state else None,
            lambda depth: _load_frame(upload_id, depth),
        )
        try:
            parser.feed(chunk)
            if index + 1 == upload["chunk_count"]:
                parser.finish()
        except InvalidRecipe as exc:
            db.execute(
                "UPDATE extension_recipe_uploads SET status = 'rejected', error = %s WHERE id = %s",
                (str(exc), upload_id),
            )
            return
        digest = advance_digest(digest, index, chunk)
        if index + 1 == upload["chunk_count"]:
            _publish(upload_id, upload["adapter"], upload["published_by"], digest.hex())
            return
        if parser.deleted_from is not None:
            db.execute(
                "DELETE FROM extension_recipe_frames WHERE upload_id = %s AND depth >= %s",
                (upload_id, parser.deleted_from),
            )
        frames = parser.changed_frames()
        if frames:
            db.executemany(
                "INSERT INTO extension_recipe_frames (upload_id, depth, body) VALUES (%s, %s, %s) "
                "ON CONFLICT (upload_id, depth) DO UPDATE SET body = EXCLUDED.body",
                [(upload_id, depth, db.jsonb(body)) for depth, body in frames.items()],
            )
        db.execute(
            "UPDATE extension_recipe_uploads SET next_chunk = %s, validation_state = %s "
            "WHERE id = %s",
            (
                index + 1,
                db.jsonb({"parser": parser.checkpoint(), "digest": digest.hex()}),
                upload_id,
            ),
        )


def _load_frame(upload_id: uuid.UUID, depth: int) -> dict | None:
    row = db.query_one(
        "SELECT body FROM extension_recipe_frames WHERE upload_id = %s AND depth = %s",
        (upload_id, depth),
    )
    return row["body"] if row else None


def _publish(upload_id: uuid.UUID, adapter: str, published_by: str | None, revision: str) -> None:
    db.query_one(
        "SELECT pg_advisory_xact_lock(hashtext('extension_recipe:' || %s))",
        (adapter,),
    )
    current = db.query_one(
        "SELECT id FROM extension_recipes WHERE adapter = %s AND revision = %s FOR UPDATE",
        (adapter, revision),
    )
    if current is None:
        db.execute(
            "INSERT INTO extension_recipes "
            "(adapter, revision, upload_id, published_by, enabled) "
            "VALUES (%s, %s, %s, %s, TRUE)",
            (adapter, revision, upload_id, published_by),
        )
    else:
        db.execute(
            "UPDATE extension_recipes SET enabled = TRUE, published_at = now(), "
            "published_by = %s WHERE id = %s",
            (published_by, current["id"]),
        )
    db.execute(
        "UPDATE extension_recipes SET enabled = FALSE WHERE adapter = %s AND revision <> %s",
        (adapter, revision),
    )
    db.execute(
        "UPDATE extension_recipe_uploads SET status = 'published', revision = %s, next_chunk = chunk_count "
        "WHERE id = %s",
        (revision, upload_id),
    )
    db.execute("DELETE FROM extension_recipe_frames WHERE upload_id = %s", (upload_id,))
