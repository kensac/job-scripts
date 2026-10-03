"""Retry one restored payload failure while preserving its original task identity."""

from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", type=int)
    args = parser.parse_args()
    if args.task_id <= 0:
        parser.error("task_id must be positive")
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 -c application_name=payload_task_recovery"
    )
    from core.pool import pool
    from tasks.runtime.payload_recovery import retry

    try:
        outcome = retry(args.task_id)
    finally:
        pool.close()
    print(json.dumps({"task_id": args.task_id, "outcome": outcome}))
    return 0 if outcome == "pending" else 1


if __name__ == "__main__":
    raise SystemExit(main())
