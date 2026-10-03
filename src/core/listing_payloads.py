"""A listing's description and raw record: inline, or a member of a verified
bundle object.

A field held by reference has three columns set: `<field>_sha256`, the SHA-256
of `encode_payload(value)`, which also names the value's member in the bundle;
`<field>_object`, the bundle's own SHA-256; and `<field>_object_size`, its
length. The inline column then holds its default ('' or '{}'), which is not
the value. An empty description is never stored by reference: '' inline with
no reference is "the board never carried the text".
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, LiteralString

from psycopg.types.json import Jsonb

from core.payload_objects import (
    MAX_CONNECTIONS,
    BundleCache,
    PayloadStore,
    PayloadUnavailable,
    bundle_groups,
    encode_payload,
)
from core.pool import connection, in_transaction, transaction

logger = logging.getLogger(__name__)

FIELDS: tuple[LiteralString, ...] = ("description", "raw")
_TYPES = {"description": str, "raw": dict}
# Everything resolve needs, for a reader's SELECT list. Never select the
# inline columns alone: a reference-held row reads '' and '{}' there.
COLUMNS: LiteralString = ", ".join(
    f"{field}, {field}_sha256, {field}_object, {field}_object_size" for field in FIELDS
)


def resolve(
    rows: Iterable[Mapping[str, Any]],
    store: PayloadStore | None = None,
    cache: BundleCache | None = None,
) -> list[dict[str, Any]]:
    """Each row with `description` and `raw` as written, the one reader of both
    shapes. A reference that cannot be read, or reads something else, raises
    PayloadUnavailable; it is never a listing without text. A caller resolving
    several batches of rows from the same bundles passes one cache."""
    cache = {} if cache is None else cache
    resolved = []
    for stored in rows:
        row = dict(stored)
        for field in FIELDS:
            if row[f"{field}_object"] is None:
                continue
            if in_transaction():
                raise RuntimeError("A listing payload cannot be read inside a database transaction")
            store = store or PayloadStore.from_env()
            value = store.get_digest_member(
                bytes(row[f"{field}_object"]).hex(),
                row[f"{field}_object_size"],
                bytes(row[f"{field}_sha256"]).hex(),
                cache,
            )
            if not isinstance(value, _TYPES[field]):
                raise PayloadUnavailable(f"Listing {field} is not a {_TYPES[field].__name__}")
            row[field] = value
        resolved.append(row)
    return resolved


def _digest(value: Any) -> str:
    return hashlib.sha256(encode_payload(value)).hexdigest()


type Link = tuple[bytes, int]


def put_values(values: Mapping[str, Any], store: PayloadStore) -> dict[str, Link]:
    """Upload values keyed by their own digest as verified bundles, split at
    BUNDLE_MAX_BYTES and uploaded concurrently up to the store's connection
    pool. Returns each digest's bundle digest and size. Outside transactions."""
    if in_transaction():
        raise RuntimeError("Listing payloads cannot be uploaded inside a database transaction")
    groups = bundle_groups(values.items(), lambda item: len(encode_payload(item[1])))
    if not groups:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(groups), MAX_CONNECTIONS)) as executor:
        uploaded = list(executor.map(lambda group: store.put_bundle(dict(group)), groups))
    return {
        name: (bytes.fromhex(ref.sha256), ref.size)
        for refs in uploaded
        for name, ref in refs.items()
    }


def reference_columns(
    listed: Sequence[tuple[str, str, dict[str, Any]]],
) -> tuple[list[tuple[Any, ...]], int]:
    """The eight payload columns record_listings writes for each (url,
    description, raw), and how many values had to stay inline.

    A value whose digest some stored row of this pull already references
    reuses that reference; every other value is uploaded, all in as few
    bundles as BUNDLE_MAX_BYTES allows. When object storage is unconfigured or
    failing those values are written inline instead, with no digest: listings
    are an archive nothing downstream reads, and failing the pull would also
    stop the catalog upsert and the page caching that ride on it. Nothing is
    left half moved, because the change check treats an inline value as
    differing from the same value referenced, so the next pull that lists the
    row moves it, and the backfill reaches a row no pull lists again.
    """
    links: dict[str, Link] = {}
    with connection() as conn:
        for row in conn.execute(
            f"SELECT {', '.join(f'{f}_sha256, {f}_object, {f}_object_size' for f in FIELDS)} "
            "FROM listings WHERE url = ANY(%s) "
            "AND (description_object IS NOT NULL OR raw_object IS NOT NULL)",
            ([url for url, _, _ in listed],),
        ):
            for field in FIELDS:
                if row[f"{field}_object"] is not None:
                    links[bytes(row[f"{field}_sha256"]).hex()] = (
                        bytes(row[f"{field}_object"]),
                        row[f"{field}_object_size"],
                    )
    digests: list[tuple[str | None, str | None]] = []
    missing: dict[str, Any] = {}
    for _, description, raw in listed:
        pair = []
        for value, empty in ((description, description == ""), (raw, False)):
            try:
                digest = None if empty else _digest(value)
            except PayloadUnavailable:
                # Not finite JSON: inline, where the database refuses it as
                # it always has.
                digest = None
            if digest is not None and digest not in links:
                missing[digest] = value
            pair.append(digest)
        digests.append((pair[0], pair[1]))
    if missing:
        try:
            links.update(put_values(missing, PayloadStore.from_env()))
        except PayloadUnavailable:
            logger.warning(f"Object storage unavailable; {len(missing)} listing values inline")
    columns = []
    inline = 0
    for (_, description, raw), pair in zip(listed, digests, strict=True):
        payload: list[Any] = []
        for value, default, digest in ((description, "", pair[0]), (raw, {}, pair[1])):
            if value == "" and default == "":
                payload += ["", None, None, None]
            elif digest is not None and digest in links:
                payload += [default, bytes.fromhex(digest), *links[digest]]
            else:
                inline += 1
                payload += [value, None, None, None]
        columns.append(tuple(payload))
    return columns, inline


type Mode = Literal["count", "externalize", "verify", "restore"]
# A writing invocation stops its cursor before the first page that could not
# reach object storage; resuming from `after` retries it. `changed` rows were
# rewritten or locked by a pull in between and are left for a rerun.
_FAILED = frozenset({"changed", "unavailable"})


def _inline(row: Mapping[str, Any], field: str) -> bool:
    return row[f"{field}_object"] is None and (field == "raw" or row["description"] != "")


def _state(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Everything a page decided from, compared under the lock."""
    return tuple(
        part
        for field in FIELDS
        for part in (
            encode_payload(row[field]),
            row[f"{field}_sha256"],
            row[f"{field}_object"],
            row[f"{field}_object_size"],
        )
    )


def _read(urls: list[str], *, lock: bool = False) -> dict[str, dict[str, Any]]:
    with connection() as conn:
        if lock:
            conn.execute("SET LOCAL lock_timeout = '2s'")
            conn.execute("SET LOCAL statement_timeout = '5s'")
        return {
            row["url"]: row
            for row in conn.execute(
                f"SELECT url, {COLUMNS} FROM listings WHERE url = ANY(%s) ORDER BY url"
                # A pull holding a row is about to rewrite it; skipping is
                # what keeps this from ever waiting on, or deadlocking with,
                # the upsert, which locks in the order the board listed.
                + (" FOR UPDATE SKIP LOCKED" if lock else ""),
                (urls,),
            )
        }


def _count(urls: list[str]) -> list[str]:
    with connection() as conn:
        # `description <> ''` compares lengths first and reads no TOAST.
        inline = {
            row["url"]: row["inline"]
            for row in conn.execute(
                "SELECT url, (description_object IS NULL AND description <> '') "
                "OR raw_object IS NULL AS inline FROM listings WHERE url = ANY(%s)",
                (urls,),
            )
        }
    return [
        "gone" if url not in inline else "inline" if inline[url] else "referenced" for url in urls
    ]


def _externalize(urls: list[str], store: PayloadStore) -> list[str]:
    rows = _read(urls)
    work: dict[str, list[LiteralString]] = {
        url: [f for f in FIELDS if _inline(row, f)] for url, row in rows.items()
    }
    try:
        links = put_values(
            {_digest(rows[url][f]): rows[url][f] for url, fields in work.items() for f in fields},
            store,
        )
    except PayloadUnavailable:
        return [
            "gone" if url not in rows else "unavailable" if work[url] else "referenced"
            for url in urls
        ]
    outcomes = []
    with transaction(), connection() as conn:
        locked = _read([url for url, fields in work.items() if fields], lock=True)
        for url in urls:
            if url not in rows:
                outcomes.append("gone")
            elif not work[url]:
                outcomes.append("referenced")
            elif url not in locked or _state(locked[url]) != _state(rows[url]):
                outcomes.append("changed")
            else:
                for field in work[url]:
                    digest = _digest(rows[url][field])
                    conn.execute(
                        f"UPDATE listings SET {field} = DEFAULT, {field}_sha256 = %s, "
                        f"{field}_object = %s, {field}_object_size = %s WHERE url = %s",
                        (bytes.fromhex(digest), *links[digest], url),
                    )
                outcomes.append("externalized")
    return outcomes


def _verify(urls: list[str], store: PayloadStore, cache: BundleCache) -> list[str]:
    rows = _read(urls)
    outcomes = []
    for url in urls:
        if url not in rows:
            outcomes.append("gone")
            continue
        try:
            resolve([rows[url]], store, cache)
        except PayloadUnavailable:
            outcomes.append("unavailable")
            continue
        outcomes.append("inline" if any(_inline(rows[url], f) for f in FIELDS) else "verified")
    return outcomes


def _restore(urls: list[str], store: PayloadStore) -> list[str]:
    rows = _read(urls)
    held = {url for url, row in rows.items() if any(row[f"{f}_object"] is not None for f in FIELDS)}
    try:
        values = {row["url"]: row for row in resolve([rows[url] for url in held], store)}
    except PayloadUnavailable:
        return [
            "gone" if url not in rows else "unavailable" if url in held else "inline"
            for url in urls
        ]
    outcomes = []
    with transaction(), connection() as conn:
        locked = _read(sorted(held), lock=True)
        for url in urls:
            if url not in rows:
                outcomes.append("gone")
            elif url not in held:
                outcomes.append("inline")
            elif url not in locked or _state(locked[url]) != _state(rows[url]):
                outcomes.append("changed")
            else:
                for field in FIELDS:
                    if rows[url][f"{field}_object"] is None:
                        continue
                    value = values[url][field]
                    conn.execute(
                        f"UPDATE listings SET {field} = %s, {field}_sha256 = NULL, "
                        f"{field}_object = NULL, {field}_object_size = NULL WHERE url = %s",
                        (Jsonb(value) if field == "raw" else value, url),
                    )
                outcomes.append("restored")
    return outcomes


def migrate(
    mode: Mode,
    *,
    after: str,
    limit: int,
    chunk_size: int,
    workers: int = 1,
    store: PayloadStore | None = None,
) -> dict[str, Any]:
    """Move the next `limit` listings after `after`, in url order, between
    inline values and the references the writer produces, one page of
    `chunk_size` rows per bundle and per short transaction, `workers` pages
    in flight.

    `externalize` uploads a page's inline values as bundles outside any
    transaction, then locks the rows it still finds unchanged and swaps each
    value for its reference in place. `restore` is its inverse, `verify`
    reads every reference through resolve, and `count` classifies without
    reading TOAST or objects. Nothing else in a row is touched, and a row
    already in the target shape is skipped.
    """
    if in_transaction():
        raise RuntimeError("Listing payload migration cannot run inside a database transaction")
    if mode != "count" and store is None:
        store = PayloadStore.from_env(max_connections=max(MAX_CONNECTIONS, workers))
    with connection() as conn:
        urls = [
            row["url"]
            for row in conn.execute(
                "SELECT url FROM listings WHERE url > %s ORDER BY url LIMIT %s", (after, limit)
            )
        ]
    pages = [urls[i : i + chunk_size] for i in range(0, len(urls), chunk_size)]

    def run(page: list[str]) -> list[str]:
        if mode == "count":
            return _count(page)
        assert store is not None
        if mode == "externalize":
            return _externalize(page, store)
        if mode == "verify":
            # A page reads each bundle once however many of its rows share it.
            return _verify(page, store, {})
        return _restore(page, store)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(run, pages))
    stop = len(pages)
    if mode in ("externalize", "restore"):
        stop = next((i for i, page in enumerate(results) if "unavailable" in page), stop)
    exhausted = stop == len(pages) and len(urls) < limit
    return {
        "counts": dict(Counter(outcome for page in results for outcome in page)),
        "failed": [
            url
            for page, outcomes in zip(pages, results, strict=True)
            for url, outcome in zip(page, outcomes, strict=True)
            if outcome in _FAILED
        ],
        "after": pages[stop - 1][-1] if stop else after,
        "exhausted": exhausted,
    }
