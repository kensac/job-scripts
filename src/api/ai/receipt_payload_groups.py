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
    scanned: int = 0
    verified_after: Cursor | None = None
    exhausted: bool = False


@dataclass
class ReceiptPage:
    sources: list[Source]
    cursors: list[Cursor]
    scanned: int
    blocked: str | None
    exhausted: bool


def read_group(
    *, after: Cursor | None, limit: int, byte_budget: int, inline_only: bool = False
) -> ReceiptPage:
    # Size selection and hydration share a snapshot, so a growing receipt cannot
    # bypass the memory budget between the two reads. No object IO occurs here.
    predicate = "r.response ? 'embedding_vectors_ref'"
    if inline_only:
        predicate += " AND r.response ? 'embedding_vectors'"
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        metadata = db.query(
            "WITH keys AS MATERIALIZED ("
            "SELECT r.provider_batch_id,r.custom_id "
            "FROM batch_result_receipts r JOIN tasks t ON t.id=r.task_id "
            f"WHERE {_ELIGIBLE} "
            "AND (%s::text IS NULL OR (r.provider_batch_id,r.custom_id)>(%s,%s)) "
            "ORDER BY r.provider_batch_id,r.custom_id LIMIT %s) "
            "SELECT r.provider_batch_id,r.custom_id,"
            f"({predicate}) AS candidate,"
            f"CASE WHEN {predicate} THEN octet_length(to_jsonb(r)::text) END AS row_bytes,"
            f"CASE WHEN {predicate} "
            "AND jsonb_typeof(r.response->'embedding_vectors_ref'->'size')='number' "
            "THEN (r.response->'embedding_vectors_ref'->>'size')::numeric END AS object_bytes "
            "FROM keys JOIN batch_result_receipts r USING(provider_batch_id,custom_id) "
            "ORDER BY r.provider_batch_id,r.custom_id",
            (after[0] if after else None, *(after or (None, None)), limit),
        )
        selected = []
        cursors = []
        reserved = 0
        blocked = None
        for row in metadata:
            cursor = (row["provider_batch_id"], row["custom_id"])
            if not row["candidate"]:
                cursors.append(cursor)
                continue
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
            cursors.append(cursor)
        if not selected:
            return ReceiptPage(
                [], cursors, len(metadata), blocked, len(cursors) == len(metadata) < limit
            )
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
        sources = [
            Source((row["provider_batch_id"], row["custom_id"]), row["task_id"], row["snapshot"])
            for row in snapshots
        ]
        return ReceiptPage(
            sources, cursors, len(metadata), blocked, len(cursors) == len(metadata) < limit
        )


def verify_object(source: Source, store: PayloadStore) -> tuple[str, int]:
    try:
        response = json.loads(source.snapshot)["response"]
        ref, _ = verified_vectors(response, store)
        return "verified", ref.size
    except PayloadUnavailable:
        return "unavailable", 0
