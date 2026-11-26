from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from api import data_level, db, model_calls, queue
from api.ai import request_snapshots
from core.batch import BatchResult, BatchSpec
from core.payloads.objects import (
    MAX_CONNECTIONS,
    BundleCache,
    Link,
    PayloadRef,
    PayloadStore,
    PayloadUnavailable,
    bundle_groups,
    encode_payload,
    put_values,
    shared_store,
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
    written: set[str] = set()
    with db.transaction():
        objects = {
            ref["key"]: request_snapshots.object_id(ref)
            for ref in {ref["key"]: ref for ref in refs.values()}.values()
        }
        # The bundle's fields stay on the row too while any running image
        # still reads them there (api.data_level); tasks.batch_objects
        # removes them once none does.
        compact = data_level.behind(data_level.LEVEL) == 0
        for spec in specs:
            ref = refs.get(spec.custom_id)
            if ref is not None and spec.custom_id not in written:
                stored = request_snapshots.member_fields(ref) if compact else ref
                if db.query_one(
                    "INSERT INTO batch_requests (task_id, custom_id, snapshot_ref, object_id) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (task_id,custom_id) DO NOTHING "
                    "RETURNING custom_id",
                    (task_id, spec.custom_id, db.jsonb(stored), objects[ref["key"]]),
                ):
                    written.add(spec.custom_id)
            if spec.custom_id in written:
                rows.append({"custom_id": spec.custom_id})
                continue
            row = db.query_one(
                f"SELECT q.custom_id, {request_snapshots.REF} AS snapshot_ref "
                f"FROM {request_snapshots.FROM} WHERE q.task_id=%s AND q.custom_id=%s",
                (task_id, spec.custom_id),
            )
            if row is None:
                raise RuntimeError("request snapshot was not recorded")
            rows.append(row)
    frozen = []
    cache: BundleCache = {}
    for row in rows:
        if row["custom_id"] in written:
            # This call wrote the row from a value already read back from storage.
            spec = request_snapshots.spec(row["custom_id"], fresh[row["custom_id"]])
        else:
            spec = request_snapshots.resolve(row, cache=cache)
        if spec is None:
            raise RuntimeError("request snapshot was not recorded")
        frozen.append(spec)
    return frozen


def _response(result: BatchResult, vectors: PayloadRef | None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "text": result.text,
        "usage": result.usage,
        "error": result.error,
        "finish_reason": result.finish_reason,
    }
    if vectors is not None:
        response["embedding_vectors_ref"] = dataclasses.asdict(vectors)
    return response


def output_tokens_of(response: dict[str, Any]) -> int | None:
    usage = response.get("usage")
    tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    return tokens if type(tokens) is int else None


def response_digest(response: dict[str, Any]) -> str:
    return hashlib.sha256(encode_payload(response)).hexdigest()


def checkpoint(
    task_id: int,
    results: list[BatchResult],
    unfinished: list[str],
    cache: BundleCache | None = None,
) -> None:
    """Record collected results before their provider batches stop being pending.

    Each response is a member of a verified bundle and each embedding's
    vectors a verified object, both read back before the receipt exists; the
    receipt holds the references. A storage failure raises PayloadUnavailable
    before any receipt is written or any batch ID is removed, so the batch is
    collected again from the provider once storage is back. The response
    bundles as read back are left in `cache`, for unconsumed. While an older
    image runs (api.data_level), the response is written inline instead,
    which that image reads, and tasks.receipt_responses moves it later.
    """
    if in_transaction():
        raise RuntimeError("Receipt payloads cannot be uploaded inside a database transaction")
    for result in results:
        if not result.batch_id:
            raise ValueError("a collected result must identify its provider batch")
    embedded = [result for result in results if result.embedding_vectors is not None]
    refs: dict[int, PayloadRef] = {}
    store: PayloadStore | None = None
    if embedded:
        store = shared_store()
        with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as executor:
            uploaded = executor.map(
                store.put_verified, [result.embedding_vectors for result in embedded]
            )
            refs = {id(result): ref for result, ref in zip(embedded, uploaded, strict=True)}
    responses = [_response(result, refs.get(id(result))) for result in results]
    links: dict[str, Link] = {}
    if responses and data_level.behind(data_level.LEVEL) == 0:
        links = put_values(
            {response_digest(response): response for response in responses},
            store or shared_store(),
            cache=cache,
        )
    with db.transaction():
        for result, response in zip(results, responses, strict=True):
            link = links.get(response_digest(response))
            stored = (
                (db.jsonb(response), None, None, None)
                if link is None
                else (None, bytes.fromhex(response_digest(response)), *link)
            )
            db.execute(
                "INSERT INTO batch_result_receipts (provider_batch_id, custom_id, task_id, "
                "response, response_sha256, response_object, response_object_size, "
                "finish_reason, output_tokens, model) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (provider_batch_id, custom_id) DO NOTHING",
                (
                    result.batch_id,
                    result.custom_id,
                    task_id,
                    *stored,
                    result.finish_reason,
                    output_tokens_of(response),
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


def resolve_responses(
    rows: list[dict[str, Any]], store: PayloadStore | None = None, cache: BundleCache | None = None
) -> list[dict[str, Any]]:
    """Each receipt row with `response` holding its response, read from its
    bundle where the row holds a reference. Bundles are read concurrently,
    once each, outside any transaction; an inline row costs nothing."""
    cache = {} if cache is None else cache
    resolved = [dict(row) for row in rows]
    held = [row for row in resolved if row["response"] is None]
    if not held:
        return resolved
    if in_transaction():
        raise RuntimeError("Receipt responses cannot be read inside a database transaction")
    store = store or shared_store()
    store.prefetch(
        ((row["response_object"].hex(), row["response_object_size"]) for row in held), cache
    )
    for row in held:
        value = store.get_digest_member(
            row["response_object"].hex(),
            row["response_object_size"],
            row["response_sha256"].hex(),
            cache,
        )
        if not isinstance(value, dict):
            raise PayloadUnavailable("Receipt response is not an object")
        row["response"] = value
    return resolved


def response_payload(response: dict[str, Any], store: PayloadStore | None = None) -> dict[str, Any]:
    payload = dict(response)
    reference = payload.pop("embedding_vectors_ref", None)
    if reference is not None:
        # An unavailable required input raises before consumption or accounting.
        # Consumed receipts are never hydrated by the replay path.
        payload["embedding_vectors"] = (store or shared_store()).get(PayloadRef.parse(reference))
    return payload


def unconsumed(task_id: int, cache: BundleCache | None = None) -> list[BatchResult]:
    cache = {} if cache is None else cache
    rows = db.query(
        f"SELECT r.*, {request_snapshots.REF} AS snapshot_ref FROM batch_result_receipts r "
        f"LEFT JOIN ({request_snapshots.FROM}) "
        "ON q.task_id=r.task_id AND q.custom_id=r.custom_id "
        "WHERE r.task_id=%s AND r.consumed_at IS NULL ORDER BY r.provider_batch_id,r.custom_id",
        (task_id,),
    )
    store = shared_store() if any(row["response"] is None for row in rows) else None
    return [
        BatchResult(
            custom_id=row["custom_id"],
            batch_id=row["provider_batch_id"],
            model=row["model"],
            request=request_snapshots.resolve(row, cache=cache),
            **response_payload(row["response"], store),
        )
        for row in resolve_responses(rows, store, cache)
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
