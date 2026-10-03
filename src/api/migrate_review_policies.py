"""Bounded, verified policy normalization and exact inline restoration."""

from __future__ import annotations

import argparse
import json
import os
from typing import cast


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("copy", "verify", "compact", "restore"))
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--through", type=int, required=True, help="fixed high-water decision ID")
    parser.add_argument(
        "--limit", type=int, required=True, help="maximum decisions this invocation"
    )
    parser.add_argument("--backup-complete", action="store_true")
    parser.add_argument(
        "--compatible-readers",
        action="store_true",
        help="confirm every API and worker reads shared policy snapshots",
    )
    args = parser.parse_args()
    if args.after < 0 or args.through < args.after or args.limit <= 0:
        parser.error("require 0 <= after <= through and a positive limit")
    if args.mode == "compact" and not (args.backup_complete and args.compatible_readers):
        parser.error("compaction requires --backup-complete and --compatible-readers")
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=review_policy_migration"
        + (" -c default_transaction_read_only=on" if args.mode == "verify" else "")
    )
    from psycopg import Error

    from api.review_policy_storage import Mode, PolicySnapshotUnavailable, migrate_chunk
    from core.pool import pool

    try:
        try:
            result = migrate_chunk(
                after=args.after,
                through=args.through,
                limit=args.limit,
                mode=cast(Mode, args.mode),
                backup_complete=args.backup_complete,
                compatible_readers=args.compatible_readers,
            )
        except (PolicySnapshotUnavailable, Error) as error:
            print(
                json.dumps(
                    {"error": type(error).__name__, "after": args.after, "through": args.through}
                )
            )
            return 1
        print(json.dumps({"mode": args.mode, **result}))
        return 1 if args.mode == "verify" and result["unreferenced"] else 0
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
