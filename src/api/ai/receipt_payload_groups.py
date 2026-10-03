"""Byte-bounded receipt evidence and shared object verification."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from api import db
from api.ai.receipt_payloads import _ELIGIBLE, verified_vectors
from core.payload_objects import PayloadStore, PayloadUnavailable

Cursor = tuple[str, str]


@dataclass(frozen=True)
class Source:
    cursor: Cursor
    task_id: int
    snapshot: str


@dataclass
class ReceiptProgress:
    after: Cursor | None
    counts: Counter[str] = field(default_factory=Counter)
    logical_bytes_verified: int = 0
    stop_reason: str | None = None


def read_group(
    *, after: Cursor | None, limit: int, byte_budget: int, inline_only: bool = False
) -> tuple[list[Source], str | None]:
    # Size selection and hydration share a snapshot, so a growing receipt cannot
    # bypass the memory budget between the two reads. No object IO occurs here.
    inline = "AND r.response ? 'embedding_vectors' " if inline_only else ""
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        metadata = db.query(
            "SELECT r.provider_batch_id,r.custom_id,"
            "octet_length(to_jsonb(r)::text) AS row_bytes,"
            "CASE WHEN jsonb_typeof(r.response->'embedding_vectors_ref'->'size')='number' "
            "THEN (r.response->'embedding_vectors_ref'->>'size')::numeric END AS object_bytes "
            "FROM batch_result_receipts r JOIN tasks t ON t.id=r.task_id "
            f"WHERE {_ELIGIBLE} AND r.response ? 'embedding_vectors_ref' {inline}"
            "AND (%s::text IS NULL OR (r.provider_batch_id,r.custom_id)>(%s,%s)) "
            "ORDER BY r.provider_batch_id,r.custom_id LIMIT %s",
            (after[0] if after else None, *(after or (None, None)), limit),
        )
        selected = []
        reserved = 0
        blocked = None
        for row in metadata:
            size = row["object_bytes"]
            if not isinstance(size, Decimal) or size < 0 or size != size.to_integral_value():
                blocked = "invalid_reference_size"
                break
            weight = row["row_bytes"] + int(size)
            if reserved + weight > byte_budget:
                if not selected:
                    blocked = "byte_budget"
                break
            reserved += weight
            selected.append(row)
        if not selected:
            return [], blocked
        snapshots = db.query(
            "SELECT r.provider_batch_id,r.custom_id,r.task_id,to_jsonb(r)::text AS snapshot "
            "FROM batch_result_receipts r JOIN unnest(%s::text[],%s::text[]) "
            "AS wanted(batch_id,custom_id) ON r.provider_batch_id=wanted.batch_id "
            "AND r.custom_id=wanted.custom_id ORDER BY r.provider_batch_id,r.custom_id",
            (
                [row["provider_batch_id"] for row in selected],
                [row["custom_id"] for row in selected],
            ),
        )
        return [
            Source((row["provider_batch_id"], row["custom_id"]), row["task_id"], row["snapshot"])
            for row in snapshots
        ], blocked


def verify_object(source: Source, store: PayloadStore) -> tuple[str, int]:
    try:
        response = json.loads(source.snapshot)["response"]
        ref, _ = verified_vectors(response, store)
        return "verified", ref.size
    except PayloadUnavailable:
        return "unavailable", 0
