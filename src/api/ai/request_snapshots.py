"""Resolve immutable request evidence before entering domain write transactions."""

from __future__ import annotations

from typing import Any

from core.batch import BatchSpec
from core.payload_objects import (
    BundleCache,
    BundleMemberRef,
    PayloadStore,
    PayloadUnavailable,
    parse_ref,
)
from core.pool import in_transaction


def load(row: dict[str, Any], store: PayloadStore, cache: BundleCache | None = None) -> Any:
    """The verified object behind a row's snapshot_ref, version 1, 2 or a bundle member."""
    ref = parse_ref(row["snapshot_ref"])
    if isinstance(ref, BundleMemberRef) and ref.member != row["custom_id"]:
        raise PayloadUnavailable("Bundle member belongs to another request")
    return store.get_ref(ref, cache)


def digest_and_size(reference: Any) -> tuple[str, int]:
    """SHA-256 and byte size of encode_payload(snapshot) a reference stands for."""
    ref = parse_ref(reference)
    if isinstance(ref, BundleMemberRef):
        return ref.member_sha256, ref.member_size
    return ref.sha256, ref.size


def resolve(
    row: dict[str, Any], store: PayloadStore | None = None, cache: BundleCache | None = None
) -> BatchSpec | None:
    """Pass one cache to every call over a task's rows so each bundle is read once."""
    snapshot = row["snapshot"]
    reference = row.get("snapshot_ref")
    if snapshot is None and reference is not None:
        if in_transaction():
            raise RuntimeError("Request hydration cannot run inside a database transaction")
        snapshot = load(row, store or PayloadStore.from_env(), cache)
    if snapshot is None and reference is None:
        return None
    try:
        if not isinstance(snapshot, dict):
            raise ValueError("request is not an object")
        spec = BatchSpec(**snapshot)
        if not isinstance(spec.custom_id, str) or spec.custom_id != row["custom_id"]:
            raise ValueError("request identity differs")
        if not all(
            isinstance(value, str) for value in (spec.instructions, spec.input, spec.schema_name)
        ):
            raise ValueError("invalid request text")
        if not isinstance(spec.schema, dict) or (
            spec.context is not None and not isinstance(spec.context, dict)
        ):
            raise ValueError("invalid request metadata")
        if spec.endpoint not in ("/v1/responses", "/v1/embeddings"):
            raise ValueError("invalid request endpoint")
        if spec.inputs is not None and (
            not isinstance(spec.inputs, list)
            or not all(isinstance(value, str) for value in spec.inputs)
        ):
            raise ValueError("invalid embedding inputs")
        return spec
    except (TypeError, ValueError) as exc:
        raise PayloadUnavailable("Required request snapshot is invalid") from exc
