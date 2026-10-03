"""Read-only verification with bounded groups and ordered progress."""

from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal

from api import db
from api.ai.receipt_payloads import _ELIGIBLE, REFERENCE
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
from core.pool import in_transaction

Cursor = tuple[str, str]


@dataclass(frozen=True)
class Source:
    cursor: Cursor
    snapshot: str


@dataclass
class Verification:
    after: Cursor | None
    counts: Counter[str] = field(default_factory=Counter)
    logical_bytes_verified: int = 0
    stop_reason: str | None = None


def _group(
    *, after: Cursor | None, limit: int, byte_budget: int
) -> tuple[list[Source], str | None]:
    # Size selection and hydration share a snapshot, so a growing receipt cannot
    # bypass the memory budget between the two reads. No object IO occurs here.
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        metadata = db.query(
            "SELECT r.provider_batch_id,r.custom_id,"
            "octet_length(to_jsonb(r)::text) AS row_bytes,"
            "CASE WHEN jsonb_typeof(r.response->'embedding_vectors_ref'->'size')='number' "
            "THEN (r.response->'embedding_vectors_ref'->>'size')::numeric END AS object_bytes "
            "FROM batch_result_receipts r JOIN tasks t ON t.id=r.task_id "
            f"WHERE {_ELIGIBLE} AND r.response ? 'embedding_vectors_ref' "
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
            "SELECT r.provider_batch_id,r.custom_id,to_jsonb(r)::text AS snapshot "
            "FROM batch_result_receipts r JOIN unnest(%s::text[],%s::text[]) "
            "AS wanted(batch_id,custom_id) ON r.provider_batch_id=wanted.batch_id "
            "AND r.custom_id=wanted.custom_id ORDER BY r.provider_batch_id,r.custom_id",
            (
                [row["provider_batch_id"] for row in selected],
                [row["custom_id"] for row in selected],
            ),
        )
        return [
            Source((row["provider_batch_id"], row["custom_id"]), row["snapshot"])
            for row in snapshots
        ], blocked


def _object(source: Source, store: PayloadStore) -> tuple[str, int]:
    try:
        response = json.loads(source.snapshot)["response"]
        ref = PayloadRef.parse(response[REFERENCE])
        vectors = store.get(ref)
        if not isinstance(vectors, list):
            raise PayloadUnavailable("Receipt vector object is not an array")
        inline = response.get("embedding_vectors")
        if inline is not None and vectors != inline:
            raise PayloadUnavailable("Receipt vectors differ from the object")
        return "verified", ref.size
    except PayloadUnavailable:
        return "unavailable", 0


def _current(sources: list[Source]) -> dict[Cursor, bool]:
    # Compare the exact database serialization, not a reconstructed JSON value
    # or a digest alone. Return booleans so changed oversized rows are never
    # hydrated into client memory during the recheck.
    rows = db.query(
        "SELECT wanted.batch_id,wanted.custom_id,COALESCE(("
        f"{_ELIGIBLE} AND to_jsonb(r)::text=wanted.snapshot),false) AS current "
        "FROM unnest(%s::text[],%s::text[],%s::text[]) "
        "AS wanted(batch_id,custom_id,snapshot) LEFT JOIN batch_result_receipts r "
        "ON r.provider_batch_id=wanted.batch_id AND r.custom_id=wanted.custom_id "
        "LEFT JOIN tasks t ON t.id=r.task_id",
        (
            [source.cursor[0] for source in sources],
            [source.cursor[1] for source in sources],
            [source.snapshot for source in sources],
        ),
    )
    return {(row["batch_id"], row["custom_id"]): row["current"] for row in rows}


def verify(
    store: PayloadStore,
    *,
    after: Cursor | None,
    limit: int,
    group_size: int,
    workers: int,
    byte_budget: int,
) -> Verification:
    if in_transaction():
        raise RuntimeError("Payload verification cannot run inside a database transaction")
    if min(limit, group_size, workers, byte_budget) <= 0 or workers > group_size:
        raise ValueError("Positive limits and workers no greater than group size are required")
    result = Verification(after)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        remaining = limit
        while remaining:
            sources, blocked = _group(
                after=result.after, limit=min(remaining, group_size), byte_budget=byte_budget
            )
            if sources:
                # Only this byte-bounded group is submitted. Futures retain
                # scalar outcomes, never decoded vectors or source documents.
                outcomes = list(executor.map(lambda source: _object(source, store), sources))
                current = _current(sources)
                for source, (object_outcome, size) in zip(sources, outcomes, strict=True):
                    outcome = object_outcome if current.get(source.cursor, False) else "changed"
                    result.counts[outcome] += 1
                    if outcome != "verified":
                        result.stop_reason = outcome
                        return result
                    result.logical_bytes_verified += size
                    result.after = source.cursor
                    remaining -= 1
            if blocked is not None:
                result.counts["unavailable"] += 1
                result.stop_reason = blocked
                break
            if not sources:
                break
    return result
