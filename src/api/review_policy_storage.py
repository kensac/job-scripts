"""Historical policies have their own lifetime, independent of tasks and filters."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from api import db


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
