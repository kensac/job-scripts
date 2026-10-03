"""Bounded moves of listing text and raw records between inline and verified bundles."""

from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("count", "externalize", "verify", "restore"))
    parser.add_argument("--after", default="", help="resume after this url (the printed after)")
    parser.add_argument("--limit", type=int, required=True, help="maximum rows this invocation")
    parser.add_argument("--chunk-size", type=int, default=1000, help="rows per page and bundle")
    parser.add_argument("--workers", type=int, default=1, help="pages in flight")
    args = parser.parse_args()
    if args.limit <= 0 or args.chunk_size <= 0 or args.workers <= 0:
        parser.error("require a positive limit, chunk size and worker count")
    # Set before the pool is imported: a stalled client must not hold a
    # production transaction or lock open.
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=listings_migration"
        + (" -c default_transaction_read_only=on" if args.mode in ("count", "verify") else "")
    )
    from psycopg import Error

    from core.listing_payloads import migrate
    from core.payload_objects import PayloadUnavailable
    from core.pool import pool

    try:
        try:
            result = migrate(
                args.mode,
                after=args.after,
                limit=args.limit,
                chunk_size=args.chunk_size,
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
