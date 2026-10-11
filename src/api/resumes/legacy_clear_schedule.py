"""Admit one verified old resume value to the clearing queue."""

from __future__ import annotations

import uuid
from typing import Any

from api import db, queue
from api.resumes.backfill_schedule import POINTERS, SEED, TABLES, part_count


def all_values_copied() -> bool:
    for field, pointer in POINTERS.items():
        for table in TABLES.values():
            if db.query_one(
                f"SELECT 1 FROM {table} WHERE {field} IS NOT NULL AND {pointer} IS NULL LIMIT 1"
            ):
                return False
    return True


def next_value() -> tuple[str, int, str] | None:
    candidates: list[tuple[bool, Any, str, int, str]] = []
    for field, pointer in POINTERS.items():
        for kind, table in TABLES.items():
            row = db.query_one(
                f"SELECT s.id, r.attempted_at FROM {table} s "
                "LEFT JOIN resume_legacy_clear_runs r ON r.source_kind = %s "
                "AND r.source_id = s.id AND r.field = %s "
                f"AND r.source_xmin = s.xmin::text AND r.canonical_id = s.{pointer} "
                f"WHERE s.{field} IS NOT NULL AND s.{pointer} IS NOT NULL "
                "ORDER BY r.attempted_at NULLS FIRST, s.id LIMIT 1",
                (kind, field),
            )
            if row is not None:
                candidates.append(
                    (row["attempted_at"] is not None, row["attempted_at"], kind, row["id"], field)
                )
    if not candidates:
        return None
    _, _, kind, row_id, field = min(candidates, key=lambda value: (value[0], value[1] or 0))
    return kind, row_id, field


def snapshot(kind: str, row_id: int, field: str, *, lock: bool = False) -> dict[str, Any] | None:
    table, pointer = TABLES[kind], POINTERS[field]
    length = "length" if field == "text" else "octet_length"
    return db.query_one(
        f"SELECT xmin::text AS source_xmin, {length}({field}) AS total, "
        f"{pointer} AS canonical_id FROM {table} WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (row_id,),
    )


def schedule_next(cycle: str) -> None:
    if not all_values_copied():
        return
    candidate = next_value()
    if candidate is None:
        return
    kind, row_id, field = candidate
    with db.transaction():
        old = snapshot(kind, row_id, field)
        if old is None or old["canonical_id"] is None or old["total"] is None:
            return
        run_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            "job-scripts/resume-legacy-clear/v1/"
            f"{kind}/{row_id}/{field}/{old['source_xmin']}/{old['canonical_id']}",
        )
        db.execute(
            "INSERT INTO resume_legacy_clear_runs "
            "(id, source_kind, source_id, field, source_xmin, canonical_id, "
            "total, part_count, verified_digest, stage) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'verify') "
            "ON CONFLICT (id) DO NOTHING",
            (
                run_id,
                kind,
                row_id,
                field,
                old["source_xmin"],
                old["canonical_id"],
                old["total"],
                part_count(old["total"], field),
                SEED,
            ),
        )
        db.execute(
            "UPDATE resume_legacy_clear_runs SET attempted_at = now() WHERE id = %s",
            (run_id,),
        )
        queue.enqueue(
            "clear_resume_legacy",
            {"run_id": str(run_id)},
            dedupe_key=f"resume-legacy-clear:{run_id}:{cycle}",
        )
