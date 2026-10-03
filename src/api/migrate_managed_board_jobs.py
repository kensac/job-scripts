"""Bounded removal of inline candidate lists from finished managed-board runs."""

from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("count", "strip"))
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--through", type=int, required=True, help="fixed high-water task ID")
    parser.add_argument("--limit", type=int, required=True, help="maximum runs this invocation")
    args = parser.parse_args()
    if args.after < 0 or args.through < args.after or args.limit <= 0:
        parser.error("require 0 <= after <= through and a positive limit")
    os.environ["PGOPTIONS"] = (
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=managed_board_jobs_retention"
        + (" -c default_transaction_read_only=on" if args.mode == "count" else "")
    )
    from psycopg import Error

    from api.managed_board_runs import strip_finished_jobs
    from core.pool import pool

    try:
        try:
            result = strip_finished_jobs(
                after=args.after,
                through=args.through,
                limit=args.limit,
                dry_run=args.mode == "count",
            )
        except Error as error:
            print(json.dumps({"error": type(error).__name__, "after": args.after}))
            return 1
        print(json.dumps({"mode": args.mode, **result}))
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
