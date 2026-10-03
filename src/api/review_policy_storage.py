"""Historical policies have their own lifetime, independent of tasks and filters."""

from __future__ import annotations

from typing import Any, Literal

from psycopg.types.json import Jsonb

from api import db

POLICY_JOIN = "LEFT JOIN review_gate_policies p ON p.id=d.policy_id"
POLICY_COLUMNS = (
    "d.policy AS inline_policy,d.policy_id,p.id AS snapshot_id,p.policy AS snapshot_policy,"
    "(d.policy::text IS NOT DISTINCT FROM p.policy::text) AS policy_matches"
)


class PolicySnapshotUnavailable(RuntimeError):
    pass


def intern(policy: Jsonb | str) -> int:
    # JSONB's own serialization is shared by live writes and historical copies.
    # Python decoding would round historical numeric values before copying them.
    with db.transaction():
        db.execute(
            "INSERT INTO review_gate_policies(digest,policy) "
            "SELECT sha256(convert_to(v.policy::text,'UTF8')),v.policy "
            "FROM (SELECT %s::jsonb AS policy) v ON CONFLICT(digest) DO NOTHING",
            (policy,),
        )
        # A second statement sees a concurrent insert after its unique-key wait.
        # Never resolve a digest collision by substituting the other payload.
        row = db.query_one(
            "SELECT p.id,p.policy::text = v.policy::text AS exact "
            "FROM review_gate_policies p CROSS JOIN (SELECT %s::jsonb AS policy) v "
            "WHERE p.digest=sha256(convert_to(v.policy::text,'UTF8'))",
            (policy,),
        )
        if row is None or not row["exact"]:
            raise PolicySnapshotUnavailable(
                "Review policy digest does not identify its exact snapshot"
            )
        return row["id"]


def resolve(row: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(row)
    inline = resolved.pop("inline_policy")
    policy_id = resolved.pop("policy_id")
    snapshot_id = resolved.pop("snapshot_id")
    snapshot = resolved.pop("snapshot_policy")
    matches = resolved.pop("policy_matches")
    if policy_id is not None:
        if snapshot_id is None or snapshot is None:
            raise PolicySnapshotUnavailable("Referenced review policy snapshot is unavailable")
        if inline is not None and not matches:
            raise PolicySnapshotUnavailable("Inline and referenced review policies disagree")
        resolved["policy"] = snapshot
    elif inline is not None:
        resolved["policy"] = inline
    else:
        raise PolicySnapshotUnavailable("Review decision has no policy snapshot")
    return resolved


Mode = Literal["copy", "verify", "compact", "restore"]


def migrate_chunk(
    *,
    after: int,
    through: int,
    limit: int,
    mode: Mode,
    backup_complete: bool = False,
    compatible_readers: bool = False,
) -> dict[str, int]:
    if after < 0 or through < after or limit <= 0:
        raise ValueError("Require 0 <= after <= through and a positive limit")
    if mode not in ("copy", "verify", "compact", "restore"):
        raise ValueError("Unknown review policy migration mode")
    if mode == "compact" and not (backup_complete and compatible_readers):
        raise ValueError("Compaction requires a completed backup and compatible readers")
    with db.transaction():
        if mode == "verify":
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = db.query(
            "SELECT d.id,d.policy::text AS policy_text,d.policy_id "
            "FROM review_gate_decisions d WHERE d.id>%s AND d.id<=%s "
            "ORDER BY d.id LIMIT %s" + ("" if mode == "verify" else " FOR UPDATE OF d"),
            (after, through, limit),
        )
        counts = {
            "scanned": len(rows),
            "copied": 0,
            "verified": 0,
            "unreferenced": 0,
            "compacted": 0,
            "restored": 0,
            "inline_remaining": 0,
            "inline_policy_bytes_removed": 0,
        }
        if rows:
            ids = [row["id"] for row in rows]
            if mode == "copy":
                snapshots = {
                    row["policy_text"]
                    for row in rows
                    if row["policy_id"] is None and row["policy_text"] is not None
                }
                references = {policy: intern(policy) for policy in sorted(snapshots)}
                updates = [
                    (references[row["policy_text"]], row["id"])
                    for row in rows
                    if row["policy_id"] is None and row["policy_text"] is not None
                ]
                db.executemany(
                    "UPDATE review_gate_decisions SET policy_id=%s "
                    "WHERE id=%s AND policy_id IS NULL",
                    updates,
                )
                counts["copied"] = len(updates)
            if mode != "verify":
                # Keep dictionary values stable between verification and mutation.
                db.query(
                    "SELECT p.id FROM review_gate_policies p WHERE p.id IN "
                    "(SELECT policy_id FROM review_gate_decisions WHERE id=ANY(%s)) "
                    "ORDER BY p.id FOR SHARE OF p",
                    (ids,),
                )
            checked = db.query(
                "SELECT d.id,d.policy_id,p.id AS snapshot_id,"
                "d.policy IS NOT NULL AS has_inline,"
                "COALESCE(d.policy,p.policy) IS NOT NULL AS has_effective,"
                "COALESCE(pg_column_size(d.policy),0) AS inline_bytes,"
                "d.policy::text IS NOT DISTINCT FROM p.policy::text AS exact,"
                "p.digest=sha256(convert_to(p.policy::text,'UTF8')) AS digest_valid "
                f"FROM review_gate_decisions d {POLICY_JOIN} WHERE d.id=ANY(%s)",
                (ids,),
            )
            for row in checked:
                if not row["has_effective"]:
                    raise PolicySnapshotUnavailable("Review decision has no policy snapshot")
                if row["policy_id"] is None:
                    if mode == "compact":
                        raise PolicySnapshotUnavailable(
                            "Compaction requires a verified policy reference"
                        )
                    counts["unreferenced"] += 1
                elif (
                    row["snapshot_id"] is None
                    or not row["digest_valid"]
                    or (row["has_inline"] and not row["exact"])
                ):
                    raise PolicySnapshotUnavailable("Review policy reference verification failed")
                else:
                    counts["verified"] += 1
                counts["inline_remaining"] += int(row["has_inline"])
            if mode == "compact":
                counts["compacted"] = db.execute_count(
                    "UPDATE review_gate_decisions SET policy=NULL "
                    "WHERE id=ANY(%s) AND policy IS NOT NULL",
                    (ids,),
                )
                counts["inline_policy_bytes_removed"] = sum(row["inline_bytes"] for row in checked)
                counts["inline_remaining"] = 0
            elif mode == "restore":
                counts["restored"] = db.execute_count(
                    "UPDATE review_gate_decisions d SET policy=p.policy "
                    "FROM review_gate_policies p WHERE d.id=ANY(%s) "
                    "AND d.policy IS NULL AND p.id=d.policy_id",
                    (ids,),
                )
                counts["inline_remaining"] += counts["restored"]
        return {**counts, "after": rows[-1]["id"] if rows else through, "through": through}
