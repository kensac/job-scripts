"""Bounded storage operations for completed non-profile request evidence."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Literal

from api import db
from api.ai import request_snapshots
from core.payload_objects import (
    BundleCache,
    BundleMemberRef,
    PayloadStore,
    PayloadUnavailable,
    encode_payload,
    parse_ref,
)
from core.pool import in_transaction

Mode = Literal["bundle", "copy", "compact", "restore", "verify"]
STOP_OUTCOMES = frozenset(("unavailable", "changed", "ineligible"))
# A bundle is read whole to resolve one member, so it stays the size of the
# largest objects this store already serves: managed-board candidate lists of
# 7.9 to 8.6 MB per run (observability.md). The member count is bounded
# separately by the page size the operator passes.
BUNDLE_MAX_BYTES = 8 * 1024 * 1024
_ELIGIBLE = (
    "t.status='done' AND t.kind<>'classify_job_profiles' "
    "AND NOT EXISTS(SELECT 1 FROM batch_result_receipts r "
    "WHERE r.task_id=t.id AND r.consumed_at IS NULL)"
)


def candidates(*, after: tuple[int, str] | None, limit: int, mode: Mode) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    predicate = {
        "bundle": "b.snapshot IS NOT NULL AND b.snapshot_ref IS NULL",
        "copy": "b.snapshot IS NOT NULL",
        "compact": "b.snapshot IS NOT NULL AND b.snapshot_ref IS NOT NULL",
        "restore": "b.snapshot_ref IS NOT NULL",
        "verify": "b.snapshot_ref IS NOT NULL",
    }[mode]
    where = (
        f"{_ELIGIBLE} AND {predicate} AND (%s::bigint IS NULL OR (b.task_id,b.custom_id)>(%s,%s))"
    )
    cursor = (after[0] if after else None, *(after or (None, None)))
    if mode == "bundle":
        # A page never spans tasks, so every bundle holds one task's rows.
        where += (
            " AND b.task_id=(SELECT b.task_id FROM batch_requests b JOIN tasks t "
            f"ON t.id=b.task_id WHERE {where} ORDER BY b.task_id,b.custom_id LIMIT 1)"
        )
        cursor = cursor * 2
    return db.query(
        f"SELECT b.* FROM batch_requests b JOIN tasks t ON t.id=b.task_id WHERE {where} "
        "ORDER BY b.task_id,b.custom_id LIMIT %s",
        (*cursor, limit),
    )


def _current_sources(sources: list[dict[str, Any]], *, lock: bool = False) -> set[tuple[int, str]]:
    ids = sorted({source["task_id"] for source in sources})
    tasks = {
        task["id"]: task
        for task in db.query(
            "SELECT id,kind,status FROM tasks WHERE id=ANY(%s) ORDER BY id"
            + (" FOR UPDATE" if lock else ""),
            (ids,),
        )
    }
    keys = [{"task_id": source["task_id"], "custom_id": source["custom_id"]} for source in sources]
    rows = {
        (row["task_id"], row["custom_id"]): row
        for row in db.query(
            "SELECT b.* FROM batch_requests b JOIN jsonb_to_recordset(%s) "
            "AS k(task_id bigint,custom_id text) USING(task_id,custom_id) "
            "ORDER BY b.task_id,b.custom_id" + (" FOR UPDATE OF b" if lock else ""),
            (db.jsonb(keys),),
        )
    }
    pending = {
        row["task_id"]
        for row in db.query(
            "SELECT DISTINCT task_id FROM batch_result_receipts "
            "WHERE task_id=ANY(%s) AND consumed_at IS NULL",
            (ids,),
        )
    }
    return {
        (source["task_id"], source["custom_id"])
        for source in sources
        if source["task_id"] in tasks
        and tasks[source["task_id"]]["status"] == "done"
        and tasks[source["task_id"]]["kind"] != "classify_job_profiles"
        and source["task_id"] not in pending
        and rows.get((source["task_id"], source["custom_id"])) == source
    }


def _prepare(
    source: dict[str, Any], store: PayloadStore, mode: Mode, cache: BundleCache
) -> tuple[str, Any, Any]:
    inline, reference = source["snapshot"], source["snapshot_ref"]
    updated, updated_ref = inline, reference
    if mode == "restore" and reference is None and inline is not None:
        request_snapshots.resolve(source, store)
        return "verified", inline, reference
    if mode == "copy" and reference is None:
        if inline is None:
            return "ineligible", inline, reference
        request_snapshots.resolve(source, store)
        updated_ref = asdict(store.put_verified(inline))
        outcome = "copied"
    else:
        if reference is None:
            raise PayloadUnavailable("Snapshot has no verified reference; copy first")
        restored = request_snapshots.load(source, store, cache)
        request_snapshots.resolve({**source, "snapshot": restored}, store)
        if inline is not None and inline != restored:
            raise PayloadUnavailable("Snapshot differs from its object")
        outcome = "verified"
        if mode == "compact":
            updated = None
            outcome = "compacted" if inline is not None else "verified"
        elif mode == "restore":
            updated, updated_ref = restored, None
            outcome = "restored"
    return outcome, updated, updated_ref


def migrate_many(
    sources: list[dict[str, Any]], store: PayloadStore, *, mode: Mode, workers: int = 1
) -> list[str]:
    """Commit an ordered prefix, stopping before any unavailable or changed source.

    A later upload can finish before an earlier failure is known. Those objects
    remain unreferenced and safe to reuse when the failed cursor is retried.
    """
    if in_transaction():
        raise RuntimeError("Snapshot migration cannot run inside a database transaction")
    if mode not in ("copy", "compact", "restore", "verify"):
        raise ValueError("unsupported migration mode")
    if workers <= 0:
        raise ValueError("workers must be positive")
    if not sources:
        return []
    current = _current_sources(sources)
    cache: BundleCache = {}

    def fetch(ref: BundleMemberRef) -> None:
        # A failure is reported by the member's own read below, in cursor order.
        with suppress(PayloadUnavailable):
            store.get_bundle(ref, cache)

    def prepare(source: dict[str, Any]) -> tuple[str, Any, Any]:
        if (source["task_id"], source["custom_id"]) not in current:
            return "changed", None, None
        try:
            return _prepare(source, store, mode, cache)
        except PayloadUnavailable:
            return "unavailable", None, None

    bundles: dict[str, BundleMemberRef] = {}
    for source in sources:
        try:
            ref = parse_ref(source["snapshot_ref"]) if source["snapshot_ref"] else None
        except PayloadUnavailable:
            continue
        if isinstance(ref, BundleMemberRef):
            bundles.setdefault(ref.key, ref)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Read each bundle once for the whole chunk before any member uses it,
        # so concurrent members of one bundle never race to GET it twice.
        list(executor.map(fetch, bundles.values()))
        prepared = list(executor.map(prepare, sources))
    stop = next((i for i, item in enumerate(prepared) if item[0] in STOP_OUTCOMES), len(prepared))
    failure = prepared[stop][0] if stop < len(prepared) else None
    prefix = sources[:stop]
    outcomes = [item[0] for item in prepared[:stop]]
    if prefix:
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='2s'")
            db.execute("SET LOCAL statement_timeout='5s'")
            current = _current_sources(prefix, lock=mode != "verify")
            updates = []
            for index, source in enumerate(prefix):
                if (source["task_id"], source["custom_id"]) not in current:
                    outcomes = outcomes[:index]
                    failure = "changed"
                    break
                _, snapshot, reference = prepared[index]
                if mode != "verify" and (snapshot, reference) != (
                    source["snapshot"],
                    source["snapshot_ref"],
                ):
                    updates.append(
                        {
                            "task_id": source["task_id"],
                            "custom_id": source["custom_id"],
                            "snapshot": snapshot,
                            "snapshot_ref": reference,
                            "original": source["snapshot"],
                            "original_ref": source["snapshot_ref"],
                        }
                    )
            if updates:
                changed = db.execute_count(
                    "UPDATE batch_requests b SET snapshot=u.snapshot,snapshot_ref=u.snapshot_ref "
                    "FROM jsonb_to_recordset(%s) AS u(task_id bigint,custom_id text,"
                    "snapshot jsonb,snapshot_ref jsonb,original jsonb,original_ref jsonb) "
                    "WHERE b.task_id=u.task_id AND b.custom_id=u.custom_id "
                    "AND b.snapshot IS NOT DISTINCT FROM u.original "
                    "AND b.snapshot_ref IS NOT DISTINCT FROM u.original_ref",
                    (db.jsonb(updates),),
                )
                if changed != len(updates):
                    raise RuntimeError("Snapshot sources changed while locked")
    return outcomes + ([failure] if failure is not None else [])


def _bundle_groups(sources: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    size = 0
    for source in sources:
        member = len(encode_payload(source["snapshot"]))
        if not groups or size + member > BUNDLE_MAX_BYTES:
            groups.append([])
            size = 0
        groups[-1].append(source)
        size += member
    return groups


def bundle_many(
    sources: list[dict[str, Any]], store: PayloadStore, *, workers: int = 1
) -> list[str]:
    """Store one task's inline-only snapshots as bundle members, keeping inline.

    Bundles upload outside any transaction. A short locked transaction then
    attaches a member reference to each row still unchanged and eligible; a
    row that changed is skipped ("changed") and stays inline-only. An invalid
    snapshot or failed bundle stops there: outcomes end with "unavailable" at
    its first row and only rows before it can be attached. A bundle whose rows
    were skipped stays partly unreferenced; it is content-addressed.
    """
    if in_transaction():
        raise RuntimeError("Snapshot migration cannot run inside a database transaction")
    if workers <= 0:
        raise ValueError("workers must be positive")
    if not sources:
        return []
    if len({source["task_id"] for source in sources}) != 1:
        raise ValueError("A bundle holds one task's snapshots")
    current = _current_sources(sources)
    skipped: dict[str, str] = {}
    valid: list[dict[str, Any]] = []
    for source in sources:
        if (
            (source["task_id"], source["custom_id"]) not in current
            or source["snapshot"] is None
            or source["snapshot_ref"] is not None
        ):
            skipped[source["custom_id"]] = "changed"
            continue
        try:
            request_snapshots.resolve(source, store)
        except PayloadUnavailable:
            skipped[source["custom_id"]] = "unavailable"
            break
        valid.append(source)

    def upload(group: list[dict[str, Any]]) -> dict[str, BundleMemberRef] | None:
        try:
            return store.put_bundle({source["custom_id"]: source["snapshot"] for source in group})
        except PayloadUnavailable:
            return None

    groups = _bundle_groups(valid)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        uploaded = list(executor.map(upload, groups))
    refs: dict[str, BundleMemberRef] = {}
    for group, group_refs in zip(groups, uploaded, strict=True):
        if group_refs is None:
            skipped[group[0]["custom_id"]] = "unavailable"
            break
        refs.update(group_refs)
    attached: set[str] = set()
    ready = [source for source in valid if source["custom_id"] in refs]
    if ready:
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='2s'")
            db.execute("SET LOCAL statement_timeout='5s'")
            locked = _current_sources(ready, lock=True)
            updates = [
                {
                    "task_id": source["task_id"],
                    "custom_id": source["custom_id"],
                    "snapshot_ref": asdict(refs[source["custom_id"]]),
                    "original": source["snapshot"],
                }
                for source in ready
                if (source["task_id"], source["custom_id"]) in locked
            ]
            if updates:
                changed = db.execute_count(
                    "UPDATE batch_requests b SET snapshot_ref=u.snapshot_ref "
                    "FROM jsonb_to_recordset(%s) AS u(task_id bigint,custom_id text,"
                    "snapshot_ref jsonb,original jsonb) "
                    "WHERE b.task_id=u.task_id AND b.custom_id=u.custom_id "
                    "AND b.snapshot=u.original AND b.snapshot_ref IS NULL",
                    (db.jsonb(updates),),
                )
                if changed != len(updates):
                    raise RuntimeError("Snapshot sources changed while locked")
            attached = {update["custom_id"] for update in updates}
    outcomes = []
    for source in sources:
        custom_id = source["custom_id"]
        if custom_id in attached:
            outcomes.append("bundled")
        elif custom_id in refs:
            outcomes.append("changed")
        elif custom_id in skipped:
            outcomes.append(skipped[custom_id])
            if skipped[custom_id] == "unavailable":
                break
        else:
            break
    return outcomes


def migrate(source: dict[str, Any], store: PayloadStore, *, mode: Mode) -> str:
    outcome = migrate_many([source], store, mode=mode)[0]
    if outcome == "unavailable":
        raise PayloadUnavailable("Snapshot object is unavailable or invalid")
    return outcome


@dataclass(frozen=True)
class ManifestResult:
    counts: dict[str, int]
    logical_bytes_verified: int
    completed: int
    after: tuple[int, str] | None
    failed: tuple[int, str] | None
    exhausted: bool


def validate_manifest(entries: Any, *, limit: int) -> list[dict[str, Any]]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("Manifest limit must be positive")
    if not isinstance(entries, list) or len(entries) > limit:
        raise ValueError("Manifest must be an array within the declared limit")
    previous = None
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not {"task_id", "custom_id", "snapshot_sha256"} <= entry.keys()
            or entry.keys()
            - {"task_id", "custom_id", "snapshot_sha256", "metadata_md5", "reference"}
        ):
            raise ValueError("Invalid manifest fields")
        if (
            type(entry["task_id"]) is not int
            or entry["task_id"] <= 0
            or not isinstance(entry["custom_id"], str)
            or not entry["custom_id"]
        ):
            raise ValueError("Invalid manifest identity")
        if (
            not isinstance(entry["snapshot_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["snapshot_sha256"]) is None
        ):
            raise ValueError("Invalid snapshot digest")
        if "metadata_md5" in entry and (
            not isinstance(entry["metadata_md5"], str)
            or re.fullmatch(r"[0-9a-f]{32}", entry["metadata_md5"]) is None
        ):
            raise ValueError("Invalid metadata digest")
        if "reference" in entry:
            try:
                parse_ref(entry["reference"])
            except PayloadUnavailable as exc:
                raise ValueError("Invalid expected reference") from exc
        key = (entry["task_id"], entry["custom_id"])
        if previous is not None and key <= previous:
            raise ValueError("Manifest identities must be sorted and unique")
        previous = key
    return entries


def migrate_manifest(
    entries: Any,
    store: PayloadStore,
    *,
    mode: Mode,
    limit: int,
    workers: int = 1,
    backup_complete: bool = False,
) -> ManifestResult:
    """Operate only on named evidence; missing rows are failures, never filters.

    The snapshot digest uses encode_payload both before copying and after reading
    an object. A reference may be added after copy to freeze its exact location.
    Exhaustion describes this supplied manifest batch, not the database catalog.
    """
    if in_transaction():
        raise RuntimeError("Snapshot migration cannot run inside a database transaction")
    entries = validate_manifest(entries, limit=limit)
    if mode not in ("copy", "compact", "restore", "verify") or workers <= 0:
        raise ValueError("Invalid manifest mode or workers")
    if mode == "compact" and not backup_complete:
        raise ValueError("Compaction requires backup confirmation")
    if not entries:
        return ManifestResult({}, 0, 0, None, None, True)
    loaded = db.query(
        "SELECT b.*,md5((to_jsonb(b)-'snapshot'-'snapshot_ref')::text) AS manifest_metadata "
        "FROM batch_requests b JOIN jsonb_to_recordset(%s) "
        "AS k(task_id bigint,custom_id text) USING(task_id,custom_id) "
        "ORDER BY b.task_id,b.custom_id",
        (db.jsonb([{"task_id": e["task_id"], "custom_id": e["custom_id"]} for e in entries]),),
    )
    rows = {(r["task_id"], r["custom_id"]): r for r in loaded}
    sources = []
    rejected = None
    reason = None
    for entry in entries:
        key = (entry["task_id"], entry["custom_id"])
        source = rows.get(key)
        if source is None:
            rejected, reason = key, "missing"
            break
        metadata = source.pop("manifest_metadata")
        try:
            actual = (
                hashlib.sha256(encode_payload(source["snapshot"])).hexdigest()
                if source["snapshot"] is not None
                else request_snapshots.digest_and_size(source["snapshot_ref"])[0]
            )
        except PayloadUnavailable:
            rejected, reason = key, "unavailable"
            break
        if (
            actual != entry["snapshot_sha256"]
            or ("metadata_md5" in entry and metadata != entry["metadata_md5"])
            or (
                "reference" in entry
                and not (mode == "restore" and source["snapshot_ref"] is None)
                and source["snapshot_ref"] != entry["reference"]
            )
        ):
            rejected, reason = key, "changed"
            break
        sources.append(source)
    outcomes = migrate_many(sources, store, mode=mode, workers=workers)
    counts: Counter[str] = Counter()
    completed = logical_bytes = 0
    after = failed = None
    for source, outcome in zip(sources, outcomes, strict=False):
        counts[outcome] += 1
        key = (source["task_id"], source["custom_id"])
        if outcome in STOP_OUTCOMES:
            failed = key
            break
        completed += 1
        after = key
        logical_bytes += (
            len(encode_payload(source["snapshot"]))
            if outcome == "copied" or source["snapshot_ref"] is None
            else request_snapshots.digest_and_size(source["snapshot_ref"])[1]
        )
    if failed is None and rejected is not None:
        assert reason is not None
        counts[reason] += 1
        failed = rejected
    return ManifestResult(
        dict(counts),
        logical_bytes,
        completed,
        after,
        failed,
        failed is None and completed == len(entries),
    )
