"""Resolve immutable request evidence before entering domain write transactions."""

from __future__ import annotations

from typing import Any

from core.batch import BatchSpec
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
from core.pool import in_transaction


def resolve(row: dict[str, Any], store: PayloadStore | None = None) -> BatchSpec | None:
    snapshot = row["snapshot"]
    reference = row.get("snapshot_ref")
    if snapshot is None and reference is not None:
        if in_transaction():
            raise RuntimeError("Request hydration cannot run inside a database transaction")
        snapshot = (store or PayloadStore.from_env()).get(PayloadRef.parse(reference))
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
