"""Validate and publish recipe uploads without assembling their bytes in memory."""

from __future__ import annotations

import hashlib
import tempfile
import uuid

from api import db
from api.apply.recipe_json import InvalidRecipe, validate


def process(upload_id: uuid.UUID) -> None:
    """One worker task copies bounded chunks to disk, then validates and publishes."""
    upload = db.query_one(
        "SELECT adapter, chunk_count, published_by, status FROM extension_recipe_uploads WHERE id = %s",
        (upload_id,),
    )
    if upload is None or upload["status"] != "pending":
        return
    digest = hashlib.sha256()
    try:
        with tempfile.TemporaryFile() as spool:
            for index in range(upload["chunk_count"]):
                row = db.query_one(
                    "SELECT body FROM extension_recipe_chunks WHERE upload_id = %s AND chunk_index = %s",
                    (upload_id, index),
                )
                if row is None:
                    raise ValueError("recipe upload is incomplete")
                part = bytes(row["body"])
                digest.update(part)
                spool.write(part)
            spool.seek(0)
            validate(spool, upload["adapter"])
        revision = digest.hexdigest()
        with db.transaction():
            db.query_one(
                "SELECT pg_advisory_xact_lock(hashtext('extension_recipe:' || %s))",
                (upload["adapter"],),
            )
            current = db.query_one(
                "SELECT id FROM extension_recipes WHERE adapter = %s AND revision = %s FOR UPDATE",
                (upload["adapter"], revision),
            )
            if current is None:
                db.execute(
                    "INSERT INTO extension_recipes "
                    "(adapter, revision, body, upload_id, published_by, enabled) "
                    "VALUES (%s, %s, NULL, %s, %s, TRUE)",
                    (upload["adapter"], revision, upload_id, upload["published_by"]),
                )
            else:
                db.execute(
                    "UPDATE extension_recipes SET enabled = TRUE, published_at = now(), "
                    "published_by = %s WHERE id = %s",
                    (upload["published_by"], current["id"]),
                )
            db.execute(
                "UPDATE extension_recipes SET enabled = FALSE "
                "WHERE adapter = %s AND revision <> %s",
                (upload["adapter"], revision),
            )
            db.execute(
                "UPDATE extension_recipe_uploads SET status = 'published', revision = %s "
                "WHERE id = %s AND status = 'pending'",
                (revision, upload_id),
            )
    except InvalidRecipe as exc:
        db.execute(
            "UPDATE extension_recipe_uploads SET status = 'rejected', error = %s "
            "WHERE id = %s AND status = 'pending'",
            (str(exc), upload_id),
        )


async def handle_publish_recipe(payload: dict[str, str]) -> None:
    process(uuid.UUID(payload["upload_id"]))
