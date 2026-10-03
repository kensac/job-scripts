from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from api import db
from api.ai import request_snapshots
from core.batch import BatchResult, BatchSpec
from core.payload_objects import MAX_CONNECTIONS, PayloadRef, PayloadStore, encode_payload
from core.pool import in_transaction


def _snapshot(spec: BatchSpec) -> Any:
    snapshot = dataclasses.asdict(spec)
    if spec.endpoint == "/v1/responses":
        snapshot.pop("endpoint")
    if spec.inputs is None:
        snapshot.pop("inputs")
    # The stored form is JSON, so freeze and verify the value JSON gives back.
    return json.loads(encode_payload(snapshot))


def snapshot_specs(task_id: int, specs: list[BatchSpec]) -> list[BatchSpec]:
    """Freeze requests before paid submission; an existing request always wins.

    New snapshots are verified objects before their reference row exists, so a
    storage failure raises PayloadUnavailable before anything is submitted.
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
            fresh.setdefault(spec.custom_id, _snapshot(spec))
    refs: dict[str, dict[str, Any]] = {}
    if fresh:
        store = PayloadStore.from_env()
        with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as executor:
            uploaded = executor.map(store.put_verified, fresh.values())
            refs = {
                custom_id: dataclasses.asdict(ref)
                for custom_id, ref in zip(fresh, uploaded, strict=True)
            }
    rows = []
    with db.transaction():
        for spec in specs:
            ref = refs.get(spec.custom_id)
            if ref is None:
                row = db.query_one(
                    "SELECT custom_id,snapshot,snapshot_ref FROM batch_requests "
                    "WHERE task_id=%s AND custom_id=%s",
                    (task_id, spec.custom_id),
                )
            else:
                row = db.query_one(
                    "INSERT INTO batch_requests (task_id, custom_id, snapshot_ref) VALUES (%s,%s,%s) "
                    "ON CONFLICT (task_id,custom_id) DO UPDATE SET snapshot=batch_requests.snapshot "
                    "RETURNING custom_id,snapshot,snapshot_ref",
                    (task_id, spec.custom_id, db.jsonb(ref)),
                )
            if row is None:
                raise RuntimeError("request snapshot was not recorded")
            rows.append(row)
    frozen = []
    for row in rows:
        source = row
        if row["snapshot"] is None and row["snapshot_ref"] == refs.get(row["custom_id"]):
            # This call wrote the row from a value already read back from storage.
            source = {**row, "snapshot": fresh[row["custom_id"]]}
        spec = request_snapshots.resolve(source)
        if spec is None:
            raise RuntimeError("cannot resubmit a legacy request without its original snapshot")
        frozen.append(spec)
    return frozen


def checkpoint(task_id: int, results: list[BatchResult], unfinished: list[str]) -> None:
    with db.transaction():
        for result in results:
            if not result.batch_id:
                raise ValueError("a collected result must identify its provider batch")
            response = {
                "text": result.text,
                "usage": result.usage,
                "error": result.error,
                "finish_reason": result.finish_reason,
            }
            if result.embedding_vectors is not None:
                response["embedding_vectors"] = result.embedding_vectors
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
        db.execute(
            "UPDATE tasks SET payload=jsonb_set(COALESCE(payload,'{}'::jsonb),'{batch_ids}',%s) "
            "|| '{\"batch_collection_checkpointed\":true}'::jsonb WHERE id=%s",
            (db.jsonb(unfinished), task_id),
        )


def response_payload(response: dict[str, Any], store: PayloadStore | None = None) -> dict[str, Any]:
    payload = dict(response)
    external = "embedding_vectors_ref" in payload
    reference = payload.pop("embedding_vectors_ref", None)
    if external and payload.get("embedding_vectors") is None:
        # An unavailable required input raises before consumption or accounting.
        # Consumed receipts are never hydrated by the replay path.
        payload["embedding_vectors"] = (store or PayloadStore.from_env()).get(
            PayloadRef.parse(reference)
        )
    return payload


def unconsumed(task_id: int) -> list[BatchResult]:
    return [
        BatchResult(
            custom_id=row["custom_id"],
            batch_id=row["provider_batch_id"],
            model=row["model"],
            request=request_snapshots.resolve(row),
            **response_payload(row["response"]),
        )
        for row in db.query(
            "SELECT r.*, q.snapshot,q.snapshot_ref FROM batch_result_receipts r LEFT JOIN batch_requests q "
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
