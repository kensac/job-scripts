"""Resolve immutable request evidence before entering domain write transactions.

Every request is a member of a version 3 bundle, held by reference with nothing
inline (observability.md). There is no other shape to read.
"""

from __future__ import annotations

from typing import Any

from core.batch import BatchSpec
from core.payload_objects import BundleCache, BundleMemberRef, PayloadStore, PayloadUnavailable
from core.pool import in_transaction


def load(row: dict[str, Any], store: PayloadStore, cache: BundleCache | None = None) -> Any:
    """The verified bundle member behind a row's snapshot_ref."""
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
