"""Bounded, reversible normalization of exact query instruction text."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any


def migrate_chunk(
    *,
    mode: str,
    after: int,
    through: int,
    limit: int,
    backup_complete: bool = False,
    readers_compatible: bool = False,
) -> dict[str, Any]:
    from core.pool import connection, in_transaction
    from core.query_instructions import InstructionUnavailable, intern

    if mode not in ("copy", "verify", "compact", "restore"):
        raise ValueError("Invalid instruction migration mode")
    if mode == "compact" and not (backup_complete and readers_compatible):
        raise InstructionUnavailable(
            "Compaction requires backup and compatible-reader confirmations"
        )
    if after < 0 or through < after or limit <= 0:
        raise ValueError("Require 0 <= after <= through and positive limit")
    if in_transaction():
        raise RuntimeError("Instruction migration requires its own bounded transaction")
    with connection() as conn, conn.transaction():
        if mode == "verify":
            conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='2s'")
        rows = conn.execute(
            "SELECT q.id,q.instructions,q.instructions_id,p.instructions AS shared,p.sha256 "
            "FROM ai_queries q LEFT JOIN ai_instruction_texts p ON p.id=q.instructions_id "
            "WHERE q.id>%s AND q.id<=%s ORDER BY q.id LIMIT %s"
            + (" FOR UPDATE OF q" if mode != "verify" else ""),
            (after, through, limit),
        ).fetchall()
        if mode != "verify":
            # A foreign key protects identity, not text. Hold content stable from
            # exact verification through inline removal or restoration.
            reference_ids = sorted(
                {r["instructions_id"] for r in rows if r["instructions_id"] is not None}
            )
            locked = (
                {
                    row["id"]: row
                    for row in conn.execute(
                        "SELECT id,instructions,sha256 FROM ai_instruction_texts "
                        "WHERE id=ANY(%s) ORDER BY id FOR SHARE",
                        (reference_ids,),
                    ).fetchall()
                }
                if reference_ids
                else {}
            )
            for row in rows:
                if row["instructions_id"] is not None:
                    shared = locked.get(row["instructions_id"])
                    row["shared"] = shared["instructions"] if shared else None
                    row["sha256"] = shared["sha256"] if shared else None
        counts = {
            "scanned": len(rows),
            "copied": 0,
            "verified": 0,
            "compacted": 0,
            "restored": 0,
            "unreferenced": 0,
            "null_instructions": 0,
        }
        references = {}
        if mode == "copy":
            for value in sorted(
                {
                    r["instructions"]
                    for r in rows
                    if r["instructions"] is not None and r["instructions_id"] is None
                }
            ):
                references[value] = intern(conn, value)
        updates = []
        logical_bytes = 0
        for row in rows:
            value, reference, shared = row["instructions"], row["instructions_id"], row["shared"]
            if reference is None:
                if value is None:
                    counts["null_instructions"] += 1
                elif mode == "copy":
                    updates.append((value, references[value], row["id"]))
                    counts["copied"] += 1
                    logical_bytes += len(value.encode("utf-8"))
                else:
                    counts["unreferenced"] += 1
                    if mode == "compact":
                        raise InstructionUnavailable(
                            "Copy and verify instructions before compaction"
                        )
                continue
            if (
                shared is None
                or hashlib.sha256(shared.encode("utf-8")).hexdigest() != row["sha256"]
            ):
                raise InstructionUnavailable("Instruction reference integrity check failed")
            if value is not None and value != shared:
                raise InstructionUnavailable("Inline and referenced instructions disagree")
            logical_bytes += len(shared.encode("utf-8"))
            counts["verified"] += 1
            if mode == "compact" and value is not None:
                updates.append((None, reference, row["id"]))
                counts["compacted"] += 1
            elif mode == "restore" and value is None:
                updates.append((shared, reference, row["id"]))
                counts["restored"] += 1
        if updates:
            with conn.cursor() as cursor:
                cursor.executemany(
                    "UPDATE ai_queries SET instructions=%s,instructions_id=%s WHERE id=%s", updates
                )
                if cursor.rowcount != len(updates):
                    raise InstructionUnavailable("Instruction migration changed-row count mismatch")
        return {
            **counts,
            "after": rows[-1]["id"] if rows else through,
            "through": through,
            "logical_bytes_verified": logical_bytes,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("copy", "verify", "compact", "restore"))
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--through", type=int, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--backup-complete", action="store_true")
    parser.add_argument("--readers-compatible", action="store_true")
    args = parser.parse_args()
    if args.after < 0 or args.through < args.after or args.limit <= 0:
        parser.error("Require 0 <= after <= through and positive limit")
    if args.mode == "compact" and not (args.backup_complete and args.readers_compatible):
        parser.error("Compaction requires --backup-complete and --readers-compatible")
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=5000 -c application_name=instruction_migration"
    )
    from psycopg import Error

    from core.pool import pool
    from core.query_instructions import InstructionUnavailable

    try:
        try:
            result = migrate_chunk(
                mode=args.mode,
                after=args.after,
                through=args.through,
                limit=args.limit,
                backup_complete=args.backup_complete,
                readers_compatible=args.readers_compatible,
            )
        except (Error, InstructionUnavailable):
            print(json.dumps({"error": "instruction_migration_failed", "after": args.after}))
            return 1
        print(json.dumps({"mode": args.mode, **result}))
        return 1 if args.mode == "verify" and result["unreferenced"] else 0
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
