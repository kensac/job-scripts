from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from api import db, model_calls, queue
from api.ai import request_snapshots
from core.batch import BatchResult, BatchSpec
from core.payload_objects import (
    MAX_CONNECTIONS,
    BundleCache,
    PayloadRef,
    PayloadStore,
    bundle_groups,
    encode_payload,
)
from core.pool import in_transaction


def _encoded(spec: BatchSpec) -> bytes:
    snapshot = dataclasses.asdict(spec)
    if spec.endpoint == "/v1/responses":
        snapshot.pop("endpoint")
    if spec.inputs is None:
        snapshot.pop("inputs")
    return encode_payload(snapshot)


def snapshot_of(spec: BatchSpec) -> Any:
    # The stored form is JSON, so freeze and verify the value JSON gives back.
    return json.loads(_encoded(spec))


def snapshot_specs(task_id: int, specs: list[BatchSpec]) -> list[BatchSpec]:
    """Freeze requests before paid submission; an existing request always wins.

    New snapshots are members of verified bundles before their reference rows
    exist, so a storage failure raises PayloadUnavailable before anything is
    submitted. Every kind is stored the same way; observability.md says why.
    """
    if in_transaction():
        raise RuntimeError("Request snapshots cannot be uploaded inside a database transaction")
    existing = {
        row["custom_id"]
        for row in db.query("SELECT custom_id FROM batch_requests WHERE task_id=%s", (task_id,))
    }
    fresh: dict[str, Any] = {}
    for spec in specs:
        if spec.custom_id not in existing:
            fresh.setdefault(spec.custom_id, snapshot_of(spec))
    refs: dict[str, dict[str, Any]] = {}
    if fresh:
        store = PayloadStore.from_env()
        with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as executor:
            groups = bundle_groups(fresh.items(), lambda item: len(encode_payload(item[1])))
            for uploaded in executor.map(store.put_bundle, map(dict, groups)):
                refs.update({name: dataclasses.asdict(ref) for name, ref in uploaded.items()})
    rows = []
    with db.transaction():
        for spec in specs:
            ref = refs.get(spec.custom_id)
            if ref is None:
                row = db.query_one(
                    "SELECT custom_id,snapshot_ref FROM batch_requests "
                    "WHERE task_id=%s AND custom_id=%s",
                    (task_id, spec.custom_id),
                )
            else:
                row = db.query_one(
                    "INSERT INTO batch_requests (task_id, custom_id, snapshot_ref) VALUES (%s,%s,%s) "
                    "ON CONFLICT (task_id,custom_id) DO UPDATE SET snapshot_ref=batch_requests.snapshot_ref "
                    "RETURNING custom_id,snapshot_ref",
                    (task_id, spec.custom_id, db.jsonb(ref)),
                )
            if row is None:
                raise RuntimeError("request snapshot was not recorded")
            rows.append(row)
    frozen = []
    cache: BundleCache = {}
    for row in rows:
        written = refs.get(row["custom_id"])
        if written is not None and row["snapshot_ref"] == written:
            # This call wrote the row from a value already read back from storage.
            spec = request_snapshots.spec(row["custom_id"], fresh[row["custom_id"]])
        else:
            spec = request_snapshots.resolve(row, cache=cache)
        if spec is None:
            raise RuntimeError("request snapshot was not recorded")
        frozen.append(spec)
    return frozen


def checkpoint(task_id: int, results: list[BatchResult], unfinished: list[str]) -> None:
    """Record collected results before their provider batches stop being pending.

    Embedding vectors are verified objects before their receipt exists, and the
    receipt holds only the reference. A storage failure raises PayloadUnavailable
    before any receipt is written or any batch ID is removed, so the batch is
    collected again from the provider once storage is back.
    """
    if in_transaction():
        raise RuntimeError("Receipt vectors cannot be uploaded inside a database transaction")
    for result in results:
        if not result.batch_id:
            raise ValueError("a collected result must identify its provider batch")
    embedded = [result for result in results if result.embedding_vectors is not None]
    refs: dict[int, PayloadRef] = {}
    if embedded:
        store = PayloadStore.from_env()
        with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as executor:
            uploaded = executor.map(
                store.put_verified, [result.embedding_vectors for result in embedded]
            )
            refs = {id(result): ref for result, ref in zip(embedded, uploaded, strict=True)}
    with db.transaction():
        for result in results:
            response: dict[str, Any] = {
                "text": result.text,
                "usage": result.usage,
                "error": result.error,
                "finish_reason": result.finish_reason,
            }
            if id(result) in refs:
                response["embedding_vectors_ref"] = dataclasses.asdict(refs[id(result)])
            db.execute(
                "INSERT INTO batch_result_receipts (provider_batch_id, custom_id, task_id, response, model) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (provider_batch_id, custom_id) DO NOTHING",
                (
                    result.batch_id,
                    result.custom_id,
                    task_id,
                    db.jsonb(response),
                    result.model,
                ),
            )
            owner = db.query_one(
                "SELECT task_id FROM batch_result_receipts WHERE provider_batch_id=%s AND custom_id=%s",
                (result.batch_id, result.custom_id),
            )
            if owner is None or owner["task_id"] != task_id:
                raise ValueError("batch result receipt belongs to another task")
        # Every collected item is paid whatever its consumer later makes of
        # it, so the ledger row is written with its receipt, not by consumers.
        model_calls.record_batch_items(results)
        queue.merge_payload(
            task_id, {"batch_ids": unfinished, "batch_collection_checkpointed": True}
        )


def response_payload(response: dict[str, Any], store: PayloadStore | None = None) -> dict[str, Any]:
    payload = dict(response)
    reference = payload.pop("embedding_vectors_ref", None)
    if reference is not None:
        # An unavailable required input raises before consumption or accounting.
        # Consumed receipts are never hydrated by the replay path.
        payload["embedding_vectors"] = (store or PayloadStore.from_env()).get(
            PayloadRef.parse(reference)
        )
    return payload


def unconsumed(task_id: int) -> list[BatchResult]:
    cache: BundleCache = {}
    return [
        BatchResult(
            custom_id=row["custom_id"],
            batch_id=row["provider_batch_id"],
            model=row["model"],
            request=request_snapshots.resolve(row, cache=cache),
            **response_payload(row["response"]),
        )
        for row in db.query(
            "SELECT r.*, q.snapshot_ref FROM batch_result_receipts r LEFT JOIN batch_requests q "
            "ON q.task_id=r.task_id AND q.custom_id=r.custom_id "
            "WHERE r.task_id=%s AND r.consumed_at IS NULL ORDER BY r.provider_batch_id,r.custom_id",
            (task_id,),
        )
    ]


def has_results(task_id: int) -> bool:
    return bool(
        db.query_one(
            "SELECT 1 FROM batch_result_receipts WHERE task_id=%s LIMIT 1",
            (task_id,),
        )
    )


@dataclasses.dataclass
class Receipt:
    pending: bool
    outcome: str = "processed"


@contextmanager
def consume_result(task_id: int, result: BatchResult) -> Iterator[Receipt]:
    with db.transaction():
        row = db.query_one(
            "SELECT outcome FROM batch_result_receipts WHERE provider_batch_id=%s AND custom_id=%s "
            "AND task_id=%s FOR UPDATE",
            (result.batch_id, result.custom_id, task_id),
        )
        if row is None:
            raise RuntimeError("result must be checkpointed before consumption")
        receipt = Receipt(pending=row["outcome"] is None, outcome=row["outcome"] or "processed")
        yield receipt
        if receipt.pending:
            db.execute(
                "UPDATE batch_result_receipts SET outcome=%s,consumed_at=now() "
                "WHERE provider_batch_id=%s AND custom_id=%s AND task_id=%s",
                (receipt.outcome, result.batch_id, result.custom_id, task_id),
            )


@contextmanager
def consume_results(task_id: int, results: list[BatchResult]) -> Iterator[list[Receipt]]:
    """consume_result for a chunk: one transaction, one lock and one acknowledgement.

    Receipts line up with results. A result collected twice is pending only
    the first time, as it would be consumed in sequence. Every result must
    hold a receipt, or the whole chunk raises before anything is written.
    """
    keys = [(result.batch_id, result.custom_id) for result in results]
    with db.transaction():
        rows = db.query(
            "SELECT provider_batch_id,custom_id,outcome FROM batch_result_receipts "
            "WHERE task_id=%s AND (provider_batch_id,custom_id) IN "
            "(SELECT * FROM unnest(%s::text[],%s::text[])) "
            "ORDER BY provider_batch_id,custom_id FOR UPDATE",
            (task_id, [key[0] for key in keys], [key[1] for key in keys]),
        )
        outcomes = {(row["provider_batch_id"], row["custom_id"]): row["outcome"] for row in rows}
        receipts = []
        seen = set()
        for key in keys:
            if key not in outcomes:
                raise RuntimeError("result must be checkpointed before consumption")
            pending = outcomes[key] is None and key not in seen
            seen.add(key)
            receipts.append(Receipt(pending=pending, outcome=outcomes[key] or "processed"))
        yield receipts
        acknowledged = [
            (key, receipt.outcome)
            for key, receipt in zip(keys, receipts, strict=True)
            if receipt.pending
        ]
        if acknowledged:
            db.execute(
                "UPDATE batch_result_receipts r SET outcome=a.outcome,consumed_at=now() "
                "FROM unnest(%s::text[],%s::text[],%s::text[]) AS a(provider_batch_id,custom_id,outcome) "
                "WHERE r.task_id=%s AND r.provider_batch_id=a.provider_batch_id "
                "AND r.custom_id=a.custom_id",
                (
                    [key[0] for key, _ in acknowledged],
                    [key[1] for key, _ in acknowledged],
                    [outcome for _, outcome in acknowledged],
                    task_id,
                ),
            )


def outcome_counts(task_id: int) -> dict[str, int]:
    return {
        row["outcome"]: row["count"]
        for row in db.query(
            "SELECT outcome, COUNT(*) AS count FROM batch_result_receipts "
            "WHERE task_id=%s AND consumed_at IS NOT NULL GROUP BY outcome",
            (task_id,),
        )
    }


def progress_counts(task_id: int, successful: tuple[str, ...] = ("written",)) -> tuple[int, int]:
    counts = outcome_counts(task_id)
    submitted = db.query_one(
        "SELECT COUNT(*) AS count FROM batch_requests WHERE task_id=%s", (task_id,)
    )
    return sum(counts.get(outcome, 0) for outcome in successful), max(
        sum(counts.values()), submitted["count"] if submitted else 0
    )
