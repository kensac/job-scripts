"""Historical policies have their own lifetime, independent of tasks and filters."""

from __future__ import annotations

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
