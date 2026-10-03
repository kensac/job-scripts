"""Bounded moves of managed-board run candidates between inline and verified objects."""

from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("count", "externalize", "verify", "restore"))
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--through", type=int, required=True, help="fixed high-water task ID")
    parser.add_argument("--limit", type=int, required=True, help="maximum runs this invocation")
    parser.add_argument("--workers", type=int, default=1, help="runs moved concurrently")
    args = parser.parse_args()
    if args.after < 0 or args.through < args.after or args.limit <= 0:
        parser.error("require 0 <= after <= through and a positive limit")
    # Each worker holds one object storage connection; beyond the pool it waits.
    from core.payload_objects import MAX_CONNECTIONS

    if not 0 < args.workers <= MAX_CONNECTIONS:
        parser.error(f"--workers must be between 1 and {MAX_CONNECTIONS}")
    # Set before the pool is imported: a stalled client must not hold a
    # production transaction or lock open.
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=managed_board_jobs_migration"
        + (" -c default_transaction_read_only=on" if args.mode in ("count", "verify") else "")
    )
    from psycopg import Error

    from api.managed_board_runs import migrate_run_jobs
    from core.payload_objects import PayloadUnavailable
    from core.pool import pool

    try:
        try:
            result = migrate_run_jobs(
                args.mode,
                after=args.after,
                through=args.through,
                limit=args.limit,
                workers=args.workers,
            )
        except (Error, PayloadUnavailable) as error:
            print(json.dumps({"error": type(error).__name__, "after": args.after}))
            return 1
        print(json.dumps({"mode": args.mode, **result}))
        return 1 if result["failed"] else 0
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
