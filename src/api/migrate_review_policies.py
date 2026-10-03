"""Copy or verify a bounded ID window; inline policies are never removed."""

from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("copy", "verify"))
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--through", type=int, required=True, help="fixed high-water decision ID")
    parser.add_argument(
        "--limit", type=int, required=True, help="maximum decisions this invocation"
    )
    args = parser.parse_args()
    if args.after < 0 or args.through < args.after or args.limit <= 0:
        parser.error("require 0 <= after <= through and a positive limit")
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=review_policy_migration"
        + (" -c default_transaction_read_only=on" if args.mode == "verify" else "")
    )
    from psycopg import Error

    from api.review_policy_storage import PolicySnapshotUnavailable, migrate_chunk
    from core.pool import pool

    try:
        try:
            result = migrate_chunk(
                after=args.after,
                through=args.through,
                limit=args.limit,
                copy=args.mode == "copy",
            )
        except (PolicySnapshotUnavailable, Error):
            print(json.dumps({"error": "policy_verification_failed", "after": args.after}))
            return 1
        print(json.dumps({"mode": args.mode, **result}))
        return 1 if args.mode == "verify" and result["unreferenced"] else 0
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
