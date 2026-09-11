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
served, an earlier one can be re-enabled (rollback), and the revision is the
digest of the canonical table, so republishing the same table is the same
revision.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from typing import Any

from pydantic import BaseModel, Field

from api import db

# Tables are hundreds of kilobytes raw (Avature 573 KB, Workday 518 KB on
# 2026-09-09); the cap leaves room without admitting an accident.
MAX_BYTES = 2_000_000
ENCODING = "deflate+base64"


class RecipeConfig(BaseModel):
    """What the extension fetches: the identity and the encoded table."""

    schema_version: int = 1
    adapter: str
    revision: str
    max_age_seconds: int
    encoding: str = ENCODING
    payload: str = Field(description="the canonical table, zlib-compressed and base64-encoded")
    digest: str = Field(description="sha256 of the canonical table; equals revision")


def canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def validate(adapter: str, body: Any) -> bytes:
    """The shape the engine requires of a table, and nothing about what the
    table says: the format is the engine's, defined in extension/engine.js.
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
    if len(raw) > MAX_BYTES:
        raise ValueError(f"recipe is {len(raw):,} bytes; the cap is {MAX_BYTES:,}")
    return raw


def revision_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(raw: bytes) -> str:
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def publish(adapter: str, body: Any, published_by: str | None) -> str:
    """Validate, store and make current. Returns the revision."""
    raw = validate(adapter, body)
    revision = revision_of(raw)
    with db.transaction():
        db.execute(
            """
            INSERT INTO extension_recipes (adapter, revision, body, published_by, enabled)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (adapter, revision) DO UPDATE
                SET enabled = TRUE, published_at = now(), published_by = EXCLUDED.published_by
            """,
            (adapter, revision, db.jsonb(body), published_by),
        )
        db.execute(
            "UPDATE extension_recipes SET enabled = FALSE WHERE adapter = %s AND revision <> %s",
            (adapter, revision),
        )
    return revision


def current(adapter: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT adapter, revision, body, published_by, published_at FROM extension_recipes "
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
        "SELECT adapter, revision, enabled, published_by, published_at, "
        "length(body::text) AS bytes FROM extension_recipes "
        "ORDER BY adapter, published_at DESC"
    )


def config_for(adapter: str, max_age_seconds: int) -> RecipeConfig | None:
    row = current(adapter)
    if not row:
        return None
    raw = canonical(row["body"])
    return RecipeConfig(
        adapter=adapter,
        revision=row["revision"],
        max_age_seconds=max_age_seconds,
        payload=encode(raw),
        digest=revision_of(raw),
    )
