from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager

from api import db
from core.batch import BatchResult, BatchSpec


def snapshot_specs(task_id: int, specs: list[BatchSpec]) -> list[BatchSpec]:
    frozen = []
    with db.transaction():
        for spec in specs:
            row = db.query_one(
                "INSERT INTO batch_requests (task_id, custom_id, snapshot) VALUES (%s,%s,%s) "
                "ON CONFLICT (task_id,custom_id) DO UPDATE SET snapshot=batch_requests.snapshot "
                "RETURNING snapshot",
                (task_id, spec.custom_id, db.jsonb(dataclasses.asdict(spec))),
            )
            if not row or row["snapshot"] is None:
                raise RuntimeError("cannot resubmit a legacy request without its original snapshot")
            frozen.append(BatchSpec(**row["snapshot"]))
    return frozen


def checkpoint(task_id: int, results: list[BatchResult], unfinished: list[str]) -> None:
    with db.transaction():
        for result in results:
            if not result.batch_id:
                raise ValueError("a collected result must identify its provider batch")
            db.execute(
                "INSERT INTO batch_result_receipts (provider_batch_id, custom_id, task_id, response, model) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (provider_batch_id, custom_id) DO NOTHING",
                (
                    result.batch_id,
                    result.custom_id,
                    task_id,
                    db.jsonb({"text": result.text, "usage": result.usage, "error": result.error}),
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
            "UPDATE tasks SET payload=jsonb_set(COALESCE(payload,'{}'::jsonb),'{batch_ids}',%s) WHERE id=%s",
            (db.jsonb(unfinished), task_id),
        )


def unconsumed(task_id: int) -> list[BatchResult]:
    return [
        BatchResult(
            custom_id=row["custom_id"],
            batch_id=row["provider_batch_id"],
            model=row["model"],
            request=BatchSpec(**row["snapshot"]) if row["snapshot"] is not None else None,
            **row["response"],
        )
        for row in db.query(
            "SELECT r.*, q.snapshot FROM batch_result_receipts r LEFT JOIN batch_requests q "
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
