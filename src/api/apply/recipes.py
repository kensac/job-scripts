"""Remote recipe tables for the extension's config-driven readers.

A recipe is the table one adapter's reader runs from: url patterns, field
selectors, fill variants, the continue and submit buttons. The extension
ships a copy of every table and runs from that copy; a table published here
wins when the extension can fetch it, so a site that changed its form is
fixed by a publish rather than a browser release (Kanishk, 2026-09-09).

The table is data the engine interprets, not code, but it does drive what
the extension touches on a page, so this channel is for a self-hosted or
developer-mode install, not the Chrome Web Store, whose policy treats a
behaviour-driving config as remote logic. The payload travels compressed and
base64-encoded: that keeps the selector tables out of casual view and out of
a public repo diff, and stops nobody who reads the extension's decoder.

Publishing keeps history: each publish is a row, the newest enabled row is
served, and an earlier one can be re-enabled (rollback). Version 1 revisions
hash canonical bytes. Version 2 revisions chain ordered chunk digests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
import zlib
from typing import Any

from pydantic import BaseModel, Field

from api import db

# The version 1 extension fetches one encoded response. Larger recipes use
# version 2 chunks; old extensions use their bundled copy for those recipes.
LEGACY_RESPONSE_BYTES = 2_000_000
CHUNK_BYTES = 64 * 1024
ENCODING = "deflate+base64"


class RecipeConfig(BaseModel):
    """What the extension fetches: the identity and the encoded table."""

    schema_version: int = 1
    adapter: str
    revision: str
    max_age_seconds: int
    encoding: str = ENCODING
    payload: str = Field(description="the recipe bytes, zlib-compressed and base64-encoded")
    digest: str = Field(description="sha256 of the served recipe bytes; equals revision")


class RecipeChunkManifest(BaseModel):
    schema_version: int = 2
    adapter: str
    revision: str
    max_age_seconds: int
    encoding: str = "identity-chunks"
    digest_algorithm: str
    chunk_count: int


class RecipeChunk(BaseModel):
    schema_version: int = 2
    adapter: str
    revision: str
    index: int
    payload: str


def canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def validate(adapter: str, body: Any) -> bytes:
    """The shape the engine requires of a table, and nothing about what the
    table says: the format is defined in extension/adapters/recipe.js.
    Returns the canonical bytes; raises ValueError with the reason."""
    if not isinstance(body, dict):
        raise ValueError("a recipe is an object")
    name = body.get("name")
    if not isinstance(name, str) or name.lower() != adapter:
        raise ValueError(f"recipe name must be the adapter id {adapter!r}, case aside")
    matches = body.get("matches")
    if (
        not isinstance(matches, list)
        or not matches
        or not all(isinstance(m, str) and m.startswith("https://") for m in matches)
    ):
        raise ValueError("matches must be a non-empty list of https:// url patterns")
    fields = body.get("fields")
    if not isinstance(fields, list):
        raise ValueError("fields must be a list")
    for i, field in enumerate(fields):
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            raise ValueError(f"fields[{i}] needs a name")
        if not isinstance(field.get("variants"), list):
            raise ValueError(f"fields[{i}] needs a variants list")
    raw = canonical(body)
    return raw


def revision_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(raw: bytes) -> str:
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def publish(adapter: str, body: Any, published_by: str | None) -> str:
    """Publish a bundled local table into the chunk store."""
    raw = validate(adapter, body)
    upload_id = uuid.uuid4()
    chunks = [raw[start : start + CHUNK_BYTES] for start in range(0, len(raw), CHUNK_BYTES)]
    with db.transaction():
        db.execute(
            "INSERT INTO extension_recipe_uploads "
            "(id, adapter, chunk_count, published_by, status) "
            "VALUES (%s, %s, %s, %s, 'pending')",
            (upload_id, adapter, len(chunks), published_by),
        )
        db.executemany(
            "INSERT INTO extension_recipe_chunks (upload_id, chunk_index, body) VALUES (%s, %s, %s)",
            [(upload_id, index, chunk) for index, chunk in enumerate(chunks)],
        )
    from api.apply.recipe_uploads import process

    for index in range(len(chunks)):
        process(upload_id, index, enqueue_next=False)
    result = db.query_one(
        "SELECT status, error, revision FROM extension_recipe_uploads WHERE id = %s", (upload_id,)
    )
    if result is None or result["status"] != "published":
        raise ValueError(result["error"] if result else "recipe upload failed")
    return result["revision"]


def current(adapter: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT adapter, revision, body, upload_id, published_by, published_at FROM extension_recipes "
        "WHERE adapter = %s AND enabled ORDER BY published_at DESC LIMIT 1",
        (adapter,),
    )


def rollback(adapter: str) -> str | None:
    """Re-enable the publish before the current one; returns its revision, or
    None when there is nothing to go back to."""
    with db.transaction():
        now = current(adapter)
        if not now:
            return None
        previous = db.query_one(
            "SELECT revision FROM extension_recipes WHERE adapter = %s AND NOT enabled "
            "AND published_at < %s ORDER BY published_at DESC LIMIT 1",
            (adapter, now["published_at"]),
        )
        if not previous:
            return None
        db.execute(
            "UPDATE extension_recipes SET enabled = (revision = %s) WHERE adapter = %s",
            (previous["revision"], adapter),
        )
    return previous["revision"]


def history() -> list[dict[str, Any]]:
    return db.query(
        "SELECT r.adapter, r.revision, r.enabled, r.published_by, r.published_at, "
        "coalesce(length(r.body::text), "
        "(SELECT sum(octet_length(c.body)) FROM extension_recipe_chunks c "
        "WHERE c.upload_id = r.upload_id)) AS bytes FROM extension_recipes r "
        "ORDER BY r.adapter, r.published_at DESC"
    )


def config_for(adapter: str, max_age_seconds: int) -> RecipeConfig | None:
    row = current(adapter)
    if not row:
        return None
    if row["body"] is not None:
        raw = canonical(row["body"])
    else:
        size = db.query_one(
            "SELECT coalesce(sum(octet_length(body)), 0) AS bytes "
            "FROM extension_recipe_chunks WHERE upload_id = %s",
            (row["upload_id"],),
        )
        if size is None or size["bytes"] > LEGACY_RESPONSE_BYTES:
            return None
        parts = db.query(
            "SELECT body FROM extension_recipe_chunks WHERE upload_id = %s ORDER BY chunk_index",
            (row["upload_id"],),
        )
        raw = b"".join(bytes(part["body"]) for part in parts)
    return RecipeConfig(
        adapter=adapter,
        revision=revision_of(raw),
        max_age_seconds=max_age_seconds,
        payload=encode(raw),
        digest=revision_of(raw),
    )


def chunk_manifest(adapter: str, max_age_seconds: int) -> RecipeChunkManifest | None:
    row = db.query_one(
        "SELECT r.revision, r.body, u.chunk_count FROM extension_recipes r "
        "LEFT JOIN extension_recipe_uploads u ON u.id = r.upload_id "
        "WHERE r.adapter = %s AND r.enabled ORDER BY r.published_at DESC LIMIT 1",
        (adapter,),
    )
    if row is None:
        return None
    chunk_count = row["chunk_count"]
    if chunk_count is None:
        if row["body"] is None:
            return None
        # Old publishes were bounded by the version 1 request limit.
        byte_count = len(canonical(row["body"]))
        chunk_count = (byte_count + CHUNK_BYTES - 1) // CHUNK_BYTES
    return RecipeChunkManifest(
        adapter=adapter,
        revision=row["revision"],
        max_age_seconds=max_age_seconds,
        chunk_count=chunk_count,
        digest_algorithm="raw-sha256" if row["body"] is not None else "chunk-chain-sha256",
    )


def chunk_for(adapter: str, revision: str, index: int) -> RecipeChunk | None:
    row = db.query_one(
        "SELECT r.body AS legacy_body, c.body AS chunk_body "
        "FROM extension_recipes r "
        "LEFT JOIN extension_recipe_chunks c ON c.upload_id = r.upload_id "
        "AND c.chunk_index = %s "
        "WHERE r.adapter = %s AND r.revision = %s",
        (index, adapter, revision),
    )
    if row is None:
        return None
    if row["chunk_body"] is not None:
        body = bytes(row["chunk_body"])
    elif row["legacy_body"] is not None:
        raw = canonical(row["legacy_body"])
        body = raw[index * CHUNK_BYTES : (index + 1) * CHUNK_BYTES]
        if not body:
            return None
    else:
        return None
    return RecipeChunk(
        adapter=adapter,
        revision=revision,
        index=index,
        payload=base64.b64encode(body).decode("ascii"),
    )
