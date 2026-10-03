"""Run explicitly bounded request snapshot storage operations, never from container startup."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from typing import cast


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bundle", "copy", "compact", "restore", "verify"))
    parser.add_argument(
        "--limit", type=int, required=True, help="maximum snapshots this invocation"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        help="maximum snapshots held and committed together (default 100; "
        "bundle: 1000, the most members one bundle holds)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="scan: pages in flight, each committing on its own; manifest: concurrent rows",
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
    if args.chunk_size is None:
        # The bundle count cannot fall below one per task (3,523 tasks held
        # the 1,081,787 rows left on 2026-10-03), so 1,000 members leaves at
        # most 1,082 bundles to save by going larger, while a page's memory
        # and its locked update grow with it.
        args.chunk_size = 1000 if args.mode == "bundle" else 100
    if args.manifest_stdin and args.mode == "bundle":
        parser.error("bundle reads its population from the database scan")
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
    from api.ai.snapshot_payloads import (
        LOCK_ERRORS,
        STOP_OUTCOMES,
        Mode,
        bundle_many,
        candidates,
        migrate_many,
    )
    from core.payload_objects import MAX_CONNECTIONS, PayloadStore, encode_payload
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
    # A bundled task's changed row is skipped, not a stop: its bundle is
    # already uploaded and the rest of the page is attached.
    stops = {"unavailable"} if mode == "bundle" else STOP_OUTCOMES
    try:
        # Object storage is bound by per-request latency, not bandwidth, so
        # throughput comes from pages in flight: each page does its object I/O
        # serially and commits on its own, and the connection pool must not
        # cap the workers below what was asked for.
        store = PayloadStore.from_env(max_connections=max(MAX_CONNECTIONS, args.workers))

        def run(rows: list[dict], before: list[Future[list[str]]]) -> list[str]:
            # Pages of one task lock the same tasks row, so a page runs only
            # after every earlier in-flight page sharing a task with it. On
            # 2026-10-03 single tasks held up to 37,329 rows (~38 bundle pages),
            # and concurrent pages of one task queued past the 2 s lock_timeout.
            # This cannot deadlock: at most `workers` pages are outstanding on
            # `workers` threads, so every page waited on is already running.
            # ponytail: a task with more pages than workers fills the window
            # and runs serially; per-task read cursors would keep other tasks
            # moving beside it.
            wait(before)
            if mode == "bundle":
                return bundle_many(rows, store)
            return migrate_many(rows, store, mode=mode)

        unread = args.limit
        read_after = after
        exhausted = False
        in_flight: deque[tuple[list[dict], Future[list[str]]]] = deque()
        last_page: dict[int, Future[list[str]]] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            while True:
                while (
                    not exhausted and stopped is None and unread and len(in_flight) < args.workers
                ):
                    rows = candidates(
                        after=read_after, limit=min(unread, args.chunk_size), mode=mode
                    )
                    if not rows:
                        exhausted = True
                        break
                    unread -= len(rows)
                    read_after = (rows[-1]["task_id"], rows[-1]["custom_id"])
                    task_ids = {row["task_id"] for row in rows}
                    before = [last_page[t] for t in task_ids if t in last_page]
                    future = executor.submit(run, rows, before)
                    last_page.update(dict.fromkeys(task_ids, future))
                    in_flight.append((rows, future))
                if not in_flight:
                    break
                # Pages are reported in cursor order. A page that commits while
                # an earlier one has failed is counted, but the cursor stays
                # before the failure: it never passes an uncommitted row, and a
                # rerun from it finds the committed rows idempotently.
                rows, page = in_flight.popleft()
                for task_id in {row["task_id"] for row in rows}:
                    if last_page.get(task_id) is page:
                        del last_page[task_id]
                try:
                    outcomes = page.result()
                except LOCK_ERRORS as exc:
                    # Retried inside the page and still locked: nothing in it
                    # committed, so stop before its first row like any failure.
                    outcomes = []
                    counts["lock_timeout"] += 1
                    stopped = stopped or "lock_timeout"
                    print(
                        json.dumps(
                            {
                                "mode": mode,
                                "error": "lock_timeout",
                                "at": [rows[0]["task_id"], rows[0]["custom_id"]],
                                "detail": str(exc).splitlines()[0],
                            }
                        ),
                        flush=True,
                    )
                for source, outcome in zip(rows, outcomes, strict=False):
                    counts[outcome] += 1
                    if outcome in stops:
                        stopped = stopped or outcome
                        break
                    if outcome in ("copied", "bundled"):
                        logical_bytes += len(encode_payload(source["snapshot"]))
                    elif source.get("snapshot_ref") is not None and outcome != "changed":
                        logical_bytes += request_snapshots.digest_and_size(source["snapshot_ref"])[
                            1
                        ]
                    if stopped is None:
                        after = (source["task_id"], source["custom_id"])
                # Emit every committed page so an interrupted long invocation
                # has a durable cursor in its captured output.
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
    return 1 if stopped is not None or counts["changed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
