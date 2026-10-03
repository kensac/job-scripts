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
    parser.add_argument("--verify-group-size", type=int)
    parser.add_argument("--verify-workers", type=int)
    parser.add_argument(
        "--verify-byte-budget",
        type=int,
        help="maximum combined serialized receipt and declared object bytes per group",
    )
    parser.add_argument("--compact-group-size", type=int)
    parser.add_argument("--compact-workers", type=int)
    parser.add_argument(
        "--compact-byte-budget",
        type=int,
        help="maximum combined serialized receipt and declared object bytes per group",
    )
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.mode == "compact" and not args.backup_complete:
        parser.error("compaction requires confirmation with --backup-complete")
    grouped = (args.verify_group_size, args.verify_workers, args.verify_byte_budget)
    if any(value is not None for value in grouped):
        if args.mode != "verify" or any(value is None or value <= 0 for value in grouped):
            parser.error(
                "grouped verification requires verify mode and all three positive verification bounds"
            )
        if args.verify_workers > args.verify_group_size:
            parser.error("verification workers cannot exceed group size")
    compact_grouped = (args.compact_group_size, args.compact_workers, args.compact_byte_budget)
    if any(value is not None for value in compact_grouped):
        if args.mode != "compact" or any(value is None or value <= 0 for value in compact_grouped):
            parser.error(
                "grouped compaction requires compact mode and all three positive compaction bounds"
            )
        if args.compact_workers > args.compact_group_size:
            parser.error("compaction workers cannot exceed group size")
    # Set connection defaults BEFORE importing the pool; no bulk read or
    # stalled client may keep a production transaction open indefinitely.
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=receipt_payload_migration"
        + (" -c default_transaction_read_only=on" if args.mode == "verify" else "")
    )
    from api.ai.receipt_payloads import Mode, candidates, migrate
    from core.payload_objects import PayloadStore, PayloadUnavailable, encode_payload
    from core.pool import pool

    mode = cast(Mode, args.mode)
    after = tuple(args.after) if args.after else None
    counts: Counter[str] = Counter()
    logical_bytes = 0
    stop_reason = None
    try:
        store = PayloadStore.from_env()
        if args.compact_group_size is not None:
            from api.ai.receipt_compaction import compact

            result = compact(
                store,
                after=after,
                limit=args.limit,
                group_size=args.compact_group_size,
                workers=args.compact_workers,
                byte_budget=args.compact_byte_budget,
                backup_complete=args.backup_complete,
            )
            counts, logical_bytes, after = (
                result.counts,
                result.logical_bytes_verified,
                result.after,
            )
            stop_reason = result.stop_reason
        elif args.verify_group_size is not None:
            from api.ai.receipt_verification import verify

            result = verify(
                store,
                after=after,
                limit=args.limit,
                group_size=args.verify_group_size,
                workers=args.verify_workers,
                byte_budget=args.verify_byte_budget,
            )
            counts, logical_bytes, after = (
                result.counts,
                result.logical_bytes_verified,
                result.after,
            )
            stop_reason = result.stop_reason
        else:
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
                if outcome in ("changed", "ineligible"):
                    stop_reason = outcome
                    break
                response = source["response"]
                if outcome == "copied":
                    logical_bytes += len(encode_payload(response["embedding_vectors"]))
                elif "embedding_vectors_ref" in response and outcome != "changed":
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
                **({"stop_reason": stop_reason} if stop_reason else {}),
            }
        )
    )
    return 1 if counts["unavailable"] or stop_reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
