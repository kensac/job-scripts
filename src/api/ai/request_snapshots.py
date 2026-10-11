"""Resolve immutable request evidence before entering domain write transactions.

Every request is a member of a version 3 bundle, held by reference with nothing
inline (observability.md). There is no other shape to read.
"""

from __future__ import annotations

from typing import Any

from api import db
from core.batch import BatchSpec
from core.payload_objects import BundleCache, BundleMemberRef, PayloadStore, PayloadUnavailable
from core.pool import in_transaction

# The bundle's fields of a member reference, held once on batch_objects. A
# member row keeps the rest (member, member_sha256, member_size). OBJECT is
# them as JSON from the batch_objects row aliased o.
OBJECT_FIELDS = ("bucket", "key", "sha256", "size", "version")
OBJECT = (
    "jsonb_build_object('bucket', o.bucket, 'key', o.key, 'sha256', o.sha256, "
    "'size', o.size, 'version', o.version)"
)

# Every reader selects snapshot_ref as REF from FROM. A row holds the bundle's
# fields itself until tasks.batch_objects removes them, and only where
# batch_objects holds the same values, so the composed reference is the one
# the row was written with before, during and after that task.
FROM = "batch_requests q LEFT JOIN batch_objects o ON o.id = q.object_id"
REF = f"q.snapshot_ref || CASE WHEN o.id IS NULL THEN '{{}}'::jsonb ELSE {OBJECT} END"
# A task's requests, each with its whole reference.
ROWS = (
    f"SELECT q.task_id, q.custom_id, {REF} AS snapshot_ref FROM {FROM} "
    "WHERE q.task_id=%s ORDER BY q.custom_id"
)


def object_id(ref: dict[str, Any]) -> int:
    """The batch_objects row describing this reference's bundle, added if new.
    The select is its own statement so it sees a row a concurrent writer
    inserted while this insert waited on (bucket, key)."""
    values = {f: ref[f] for f in OBJECT_FIELDS}
    db.execute(
        "INSERT INTO batch_objects (bucket, key, sha256, size, version) "
        "VALUES (%(bucket)s, %(key)s, %(sha256)s, %(size)s, %(version)s) "
        "ON CONFLICT (bucket, key) DO NOTHING",
        values,
    )
    row = db.query_one(
        "SELECT id FROM batch_objects WHERE bucket = %(bucket)s AND key = %(key)s "
        "AND sha256 = %(sha256)s AND size = %(size)s AND version = %(version)s",
        values,
    )
    if row is None:
        raise RuntimeError("batch_objects holds this key with other fields")
    return int(row["id"])


def member_fields(ref: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ref.items() if k not in OBJECT_FIELDS}


def load(row: dict[str, Any], store: PayloadStore, cache: BundleCache | None = None) -> Any:
    """The verified bundle member behind a row's snapshot_ref, selected as REF."""
    ref = BundleMemberRef.parse(row["snapshot_ref"])
    if ref.member != row["custom_id"]:
        raise PayloadUnavailable("Bundle member belongs to another request")
    return store.get_member(ref, cache)


def resolve(
    row: dict[str, Any], store: PayloadStore | None = None, cache: BundleCache | None = None
) -> BatchSpec | None:
    """Pass one cache to every call over a task's rows so each bundle is read once.

    None when the row holds no request (a receipt with no request row)."""
    if row.get("snapshot_ref") is None:
        return None
    if in_transaction():
        raise RuntimeError("Request hydration cannot run inside a database transaction")
    return spec(row["custom_id"], load(row, store or PayloadStore.from_env(), cache))


def spec(custom_id: str, snapshot: Any) -> BatchSpec:
    """The request a stored snapshot holds, refused unless it is exactly one."""
    try:
        if not isinstance(snapshot, dict):
            raise ValueError("request is not an object")
        result = BatchSpec(**snapshot)
        if not isinstance(result.custom_id, str) or result.custom_id != custom_id:
            raise ValueError("request identity differs")
        if not all(
            isinstance(value, str)
            for value in (result.instructions, result.input, result.schema_name)
        ):
            raise ValueError("invalid request text")
        if not isinstance(result.schema, dict) or (
            result.context is not None and not isinstance(result.context, dict)
        ):
            raise ValueError("invalid request metadata")
        if result.endpoint not in ("/v1/responses", "/v1/embeddings"):
            raise ValueError("invalid request endpoint")
        if result.inputs is not None and (
            not isinstance(result.inputs, list)
            or not all(isinstance(value, str) for value in result.inputs)
        ):
            raise ValueError("invalid embedding inputs")
        return result
    except (TypeError, ValueError) as exc:
        raise PayloadUnavailable("Required request snapshot is invalid") from exc
