"""Run explicitly bounded receipt storage operations, never from container startup."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import cast


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("copy", "compact", "restore", "verify"))
    parser.add_argument("--limit", type=int, required=True, help="maximum receipts this invocation")
    parser.add_argument("--after", nargs=2, metavar=("BATCH_ID", "CUSTOM_ID"))
    parser.add_argument(
        "--backup-complete", action="store_true", help="confirm independent DB copy finished"
    )
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.mode == "compact" and not args.backup_complete:
        parser.error("compaction requires confirmation with --backup-complete")
    # Set connection defaults BEFORE importing the pool; no bulk read or
    # stalled client may keep a production transaction open indefinitely.
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=receipt_payload_migration"
        + (" -c default_transaction_read_only=on" if args.mode == "verify" else "")
    )
    from api.ai.receipt_payloads import Mode, candidates, migrate
    from core.payload_objects import PayloadStore, PayloadUnavailable
    from core.pool import pool

    mode = cast(Mode, args.mode)
    after = tuple(args.after) if args.after else None
    counts: Counter[str] = Counter()
    logical_bytes = 0
    try:
        store = PayloadStore.from_env()
        for _ in range(args.limit):
            # One payload at a time bounds memory independently of the count.
            rows = candidates(after=after, limit=1, mode=mode)
            if not rows:
                break
            source = rows[0]
            try:
                outcome = migrate(source, store, mode=mode)
            except PayloadUnavailable:
                counts["unavailable"] += 1
                # Stop at the failed row. Resuming from the returned cursor
                # retries it rather than silently skipping missing history.
                break
            counts[outcome] += 1
            response = source["response"]
            if "embedding_vectors_ref" in response and outcome != "changed":
                logical_bytes += response["embedding_vectors_ref"]["size"]
            after = (source["provider_batch_id"], source["custom_id"])
    finally:
        pool.close()
    print(
        json.dumps(
            {
                "mode": mode,
                "counts": dict(counts),
                "logical_bytes_verified": logical_bytes,
                "after": after,
            }
        )
    )
    return 1 if counts["unavailable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
