"""Bounded storage operations for completed non-profile request evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, Literal

from api import db
from api.ai import request_snapshots
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
from core.pool import in_transaction

Mode = Literal["copy", "compact", "restore", "verify"]
STOP_OUTCOMES = frozenset(("unavailable", "changed", "ineligible"))
_ELIGIBLE = (
    "t.status='done' AND t.kind<>'classify_job_profiles' "
    "AND NOT EXISTS(SELECT 1 FROM batch_result_receipts r "
    "WHERE r.task_id=t.id AND r.consumed_at IS NULL)"
)


def candidates(*, after: tuple[int, str] | None, limit: int, mode: Mode) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    predicate = {
        "copy": "b.snapshot IS NOT NULL",
        "compact": "b.snapshot IS NOT NULL AND b.snapshot_ref IS NOT NULL",
        "restore": "b.snapshot_ref IS NOT NULL",
        "verify": "b.snapshot_ref IS NOT NULL",
    }[mode]
    return db.query(
        "SELECT b.* FROM batch_requests b JOIN tasks t ON t.id=b.task_id "
        f"WHERE {_ELIGIBLE} AND {predicate} "
        "AND (%s::bigint IS NULL OR (b.task_id,b.custom_id)>(%s,%s)) "
        "ORDER BY b.task_id,b.custom_id LIMIT %s",
        (after[0] if after else None, *(after or (None, None)), limit),
    )


def _current_sources(sources: list[dict[str, Any]], *, lock: bool = False) -> set[tuple[int, str]]:
    ids = sorted({source["task_id"] for source in sources})
    tasks = {
        task["id"]: task
        for task in db.query(
            "SELECT id,kind,status FROM tasks WHERE id=ANY(%s) ORDER BY id"
            + (" FOR UPDATE" if lock else ""),
            (ids,),
        )
    }
    keys = [{"task_id": source["task_id"], "custom_id": source["custom_id"]} for source in sources]
    rows = {
        (row["task_id"], row["custom_id"]): row
        for row in db.query(
            "SELECT b.* FROM batch_requests b JOIN jsonb_to_recordset(%s) "
            "AS k(task_id bigint,custom_id text) USING(task_id,custom_id) "
            "ORDER BY b.task_id,b.custom_id" + (" FOR UPDATE OF b" if lock else ""),
            (db.jsonb(keys),),
        )
    }
    pending = {
        row["task_id"]
        for row in db.query(
            "SELECT DISTINCT task_id FROM batch_result_receipts "
            "WHERE task_id=ANY(%s) AND consumed_at IS NULL",
            (ids,),
        )
    }
    return {
        (source["task_id"], source["custom_id"])
        for source in sources
        if source["task_id"] in tasks
        and tasks[source["task_id"]]["status"] == "done"
        and tasks[source["task_id"]]["kind"] != "classify_job_profiles"
        and source["task_id"] not in pending
        and rows.get((source["task_id"], source["custom_id"])) == source
    }


def _prepare(source: dict[str, Any], store: PayloadStore, mode: Mode) -> tuple[str, Any, Any]:
    inline, reference = source["snapshot"], source["snapshot_ref"]
    updated, updated_ref = inline, reference
    if mode == "copy" and reference is None:
        if inline is None:
            return "ineligible", inline, reference
        request_snapshots.resolve(source, store)
        updated_ref = asdict(store.put_verified(inline))
        outcome = "copied"
    else:
        if reference is None:
            raise PayloadUnavailable("Snapshot has no verified reference; copy first")
        restored = store.get(PayloadRef.parse(reference))
        request_snapshots.resolve({**source, "snapshot": restored}, store)
        if inline is not None and inline != restored:
            raise PayloadUnavailable("Snapshot differs from its object")
        outcome = "verified"
        if mode == "compact":
            updated = None
            outcome = "compacted" if inline is not None else "verified"
        elif mode == "restore":
            updated, updated_ref = restored, None
            outcome = "restored"
    return outcome, updated, updated_ref


def migrate_many(
    sources: list[dict[str, Any]], store: PayloadStore, *, mode: Mode, workers: int = 1
) -> list[str]:
    """Commit an ordered prefix, stopping before any unavailable or changed source.

    A later upload can finish before an earlier failure is known. Those objects
    remain unreferenced and safe to reuse when the failed cursor is retried.
    """
    if in_transaction():
        raise RuntimeError("Snapshot migration cannot run inside a database transaction")
    if mode not in ("copy", "compact", "restore", "verify"):
        raise ValueError("unsupported migration mode")
    if workers <= 0:
        raise ValueError("workers must be positive")
    if not sources:
        return []
    current = _current_sources(sources)

    def prepare(source: dict[str, Any]) -> tuple[str, Any, Any]:
        if (source["task_id"], source["custom_id"]) not in current:
            return "changed", None, None
        try:
            return _prepare(source, store, mode)
        except PayloadUnavailable:
            return "unavailable", None, None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        prepared = list(executor.map(prepare, sources))
    stop = next((i for i, item in enumerate(prepared) if item[0] in STOP_OUTCOMES), len(prepared))
    failure = prepared[stop][0] if stop < len(prepared) else None
    prefix = sources[:stop]
    outcomes = [item[0] for item in prepared[:stop]]
    if prefix:
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='2s'")
            db.execute("SET LOCAL statement_timeout='5s'")
            current = _current_sources(prefix, lock=mode != "verify")
            updates = []
            for index, source in enumerate(prefix):
                if (source["task_id"], source["custom_id"]) not in current:
                    outcomes = outcomes[:index]
                    failure = "changed"
                    break
                _, snapshot, reference = prepared[index]
                if mode != "verify" and (snapshot, reference) != (
                    source["snapshot"],
                    source["snapshot_ref"],
                ):
                    updates.append(
                        {
                            "task_id": source["task_id"],
                            "custom_id": source["custom_id"],
                            "snapshot": snapshot,
                            "snapshot_ref": reference,
                            "original": source["snapshot"],
                            "original_ref": source["snapshot_ref"],
                        }
                    )
            if updates:
                changed = db.execute_count(
                    "UPDATE batch_requests b SET snapshot=u.snapshot,snapshot_ref=u.snapshot_ref "
                    "FROM jsonb_to_recordset(%s) AS u(task_id bigint,custom_id text,"
                    "snapshot jsonb,snapshot_ref jsonb,original jsonb,original_ref jsonb) "
                    "WHERE b.task_id=u.task_id AND b.custom_id=u.custom_id "
                    "AND b.snapshot IS NOT DISTINCT FROM u.original "
                    "AND b.snapshot_ref IS NOT DISTINCT FROM u.original_ref",
                    (db.jsonb(updates),),
                )
                if changed != len(updates):
                    raise RuntimeError("Snapshot sources changed while locked")
    return outcomes + ([failure] if failure is not None else [])


def migrate(source: dict[str, Any], store: PayloadStore, *, mode: Mode) -> str:
    outcome = migrate_many([source], store, mode=mode)[0]
    if outcome == "unavailable":
        raise PayloadUnavailable("Snapshot object is unavailable or invalid")
    return outcome
