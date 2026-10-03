"""Run explicitly bounded request snapshot storage operations, never from container startup."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from typing import cast


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("copy", "compact", "restore", "verify"))
    parser.add_argument(
        "--limit", type=int, required=True, help="maximum snapshots this invocation"
    )
    parser.add_argument(
        "--chunk-size", type=int, default=100, help="maximum snapshots held and committed together"
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="concurrent verified object operations"
    )
    parser.add_argument(
        "--manifest-stdin",
        action="store_true",
        help="read an exact sorted evidence manifest JSON array from stdin; limit bounds its rows",
    )
    parser.add_argument("--after", nargs=2, metavar=("TASK_ID", "CUSTOM_ID"))
    parser.add_argument(
        "--backup-complete", action="store_true", help="confirm independent DB copy finished"
    )
    args = parser.parse_args()
    if args.manifest_stdin and args.after:
        parser.error("Manifest batches cannot use a database scan cursor")
    if min(args.limit, args.chunk_size, args.workers) <= 0:
        parser.error("--limit, --chunk-size and --workers must be positive")
    if args.mode == "compact" and not args.backup_complete:
        parser.error("compaction requires confirmation with --backup-complete")
    # Set connection defaults BEFORE importing the pool; no bulk read or
    # stalled client may keep a production transaction open indefinitely.
    os.environ["PGOPTIONS"] = (
        "-c statement_timeout=5000 -c lock_timeout=2000 "
        "-c idle_in_transaction_session_timeout=5000 "
        "-c application_name=snapshot_payload_migration"
        + (" -c default_transaction_read_only=on" if args.mode == "verify" else "")
    )
    from api.ai import request_snapshots
    from api.ai.snapshot_payloads import STOP_OUTCOMES, Mode, candidates, migrate_many
    from core.payload_objects import PayloadStore, encode_payload
    from core.pool import pool

    if args.manifest_stdin:
        from api.ai.snapshot_payloads import migrate_manifest, validate_manifest

        try:
            entries = validate_manifest(
                json.load(sys.stdin), limit=min(args.limit, args.chunk_size)
            )
        except (ValueError, TypeError) as exc:
            parser.error(str(exc))
        try:
            result = migrate_manifest(
                entries,
                PayloadStore.from_env(),
                mode=cast(Mode, args.mode),
                limit=args.limit,
                workers=args.workers,
                backup_complete=args.backup_complete,
            )
        finally:
            pool.close()
        print(json.dumps({"mode": args.mode, "scope": "manifest", **asdict(result)}), flush=True)
        return 0 if result.exhausted else 1

    mode = cast(Mode, args.mode)
    after = (int(args.after[0]), args.after[1]) if args.after else None
    counts: Counter[str] = Counter()
    logical_bytes = 0
    stopped = None
    try:
        store = PayloadStore.from_env()
        remaining = args.limit
        while remaining:
            rows = candidates(after=after, limit=min(remaining, args.chunk_size), mode=mode)
            if not rows:
                break
            outcomes = migrate_many(rows, store, mode=mode, workers=args.workers)
            for source, outcome in zip(rows, outcomes, strict=False):
                counts[outcome] += 1
                if outcome in STOP_OUTCOMES:
                    stopped = outcome
                    break
                if outcome == "copied":
                    logical_bytes += len(encode_payload(source["snapshot"]))
                elif source.get("snapshot_ref") is not None and outcome != "changed":
                    logical_bytes += request_snapshots.digest_and_size(source["snapshot_ref"])[1]
                after = (source["task_id"], source["custom_id"])
                remaining -= 1
            # Emit every committed chunk so an interrupted long invocation has
            # a durable cursor in its captured output, never beyond a failure.
            print(
                json.dumps(
                    {
                        "mode": mode,
                        "counts": dict(counts),
                        "logical_bytes_verified": logical_bytes,
                        "after": after,
                    }
                ),
                flush=True,
            )
            if stopped is not None:
                break
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
    return 1 if stopped is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
